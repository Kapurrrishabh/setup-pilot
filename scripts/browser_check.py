"""Click through the web app in a real browser. scripts/acceptance.py (the browser part) starts the
app and runs this file.

Run by hand: start `uv run setuppilot web`, then
uv run --group e2e python scripts/browser_check.py --work ~/setuppilot-test/browser

Screens that need GitHub sign-in, a missing tool or a failed command are drawn from made-up
answers (Playwright routes), so they need no GitHub App and change nothing. Each screen is checked
with axe in light and dark, on a desktop and a phone, and saved as a screenshot.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from playwright.sync_api import Browser, Page, Route, expect, sync_playwright

SETUP_REPO = "https://github.com/pypa/sampleproject"
CANCEL_REPO = "https://github.com/JedWatson/classnames"
WAIT_MS = 300_000
AXE_URL = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
AXE_PROBLEMS = (
    "axe.run().then(r => r.violations.map("
    "v => `${v.id}: ${v.nodes.map(n => n.target.join(' ')).join(', ')}`))"
)
# name: (color scheme, width, height)
VARIANTS: dict[str, tuple[Literal["light", "dark"], int, int]] = {
    "light-desktop": ("light", 1280, 900),
    "dark-desktop": ("dark", 1280, 900),
    "light-phone": ("light", 375, 812),
    "dark-phone": ("dark", 375, 812),
}
# No screen may scroll sideways anywhere in this range.
WIDTHS = (320, 768, 1440)
KEY = "made-up-binding-key"
problems: list[str] = []


def check(is_ok: bool, what: str) -> None:
    print(("PASS " if is_ok else "FAIL ") + what)
    if not is_ok:
        problems.append(what)


def wait_until(page: Page, condition, timeout_ms: int = WAIT_MS) -> bool:
    # The app's CSP blocks the eval that Playwright's wait_for_function needs, so poll here.
    for _ in range(timeout_ms // 500):
        if condition():
            return True
        page.wait_for_timeout(500)
    return False


def start_run(page: Page, base: str, url: str, folder: Path) -> None:
    page.goto(base + "/")
    page.fill("input[name=repo_url]", url)
    page.fill("input[name=dest_dir]", str(folder))
    page.click("#setup-form button[type=submit]")
    page.wait_for_url(re.compile(r"/runs/[0-9a-f-]+$"), timeout=30_000)


def check_setup_page(page: Page, base: str) -> None:
    page.goto(base + "/")
    check(page.locator("#repo-url").is_visible(), "the setup page starts on Paste a URL")
    page.click("label.filter:has-text('My GitHub repos')")
    off = page.locator("#gh-off")
    expect(off).to_be_visible(timeout=10_000)
    link = off.locator("a").get_attribute("href") or ""
    check(
        "Sign in with GitHub is off" in off.inner_text() and link.endswith("#sign-in-with-github"),
        "with no GitHub App, the GitHub tab says sign-in is off and links to how to turn it on",
    )
    page.click("label.filter:has-text('Paste a URL')")
    check(page.locator("#repo-url").is_visible(), "Paste a URL shows the URL box again")
    page.fill("input[name=dest_dir]", str(Path.home()))
    page.click("#setup-form button[type=submit]")
    expect(page.locator("#error")).to_be_visible()
    check("Paste the repo's GitHub URL" in page.inner_text("#error"), "no URL: it says what to do")
    has_browse = page.locator("#browse").count() == 1
    hint = page.inner_text("#dest-dir-hint")
    check(
        has_browse or "no Browse button" in hint, f"Browse is there, or the hint says why: {hint}"
    )


def check_setup_run(page: Page, base: str, folder: Path) -> None:
    start_run(page, base, SETUP_REPO, folder)
    question, report = page.locator("#question"), page.locator("#report-card")
    expect(question).to_be_visible(timeout=60_000)
    analysis = page.locator("#analysis")
    check(
        analysis.is_visible() and page.locator("#analysis-options li").count() > 0,
        "the run shows what is in the repo: "
        + " | ".join(page.locator("#analysis-options li").all_inner_texts()),
    )

    page.click("#question-actions button[data-action=edit]")
    page.fill("#edit-command", "rm -rf /")
    page.click("#edit-send")
    expect(page.locator("#question-edit-error")).to_be_visible(timeout=30_000)
    check("blocked" in page.inner_text("#question-edit-error"), "a dangerous edit is refused")

    page.click("#question-actions button[data-action=edit]")
    page.locator("#edit-command").press_sequentially(" are")
    is_still_asking = question.is_visible() and page.locator("#edit-box").is_visible()
    check(is_still_asking, "keys typed into the edit box do not answer the question")
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)  # keys are ignored for a moment after a question shows up
    first = page.inner_text("#question-command")
    page.keyboard.press("a")
    wait_until(page, lambda: report.is_visible() or page.inner_text("#question-command") != first)
    approved = page.locator("#steps li").filter(has_text=f"Approved: {first}").count()
    check(approved == 1, f"the A key approves the command on screen ({first})")

    expect(question).to_be_visible(timeout=60_000)
    page.wait_for_timeout(800)
    second = page.inner_text("#question-command")
    page.focus("#repo")
    page.keyboard.press("r")
    page.wait_for_timeout(1500)
    is_unanswered = question.is_visible() and page.inner_text("#question-command") == second
    check(is_unanswered, "a key pressed outside the question answers nothing")

    approvals = 1
    while approvals < 10:
        wait_until(page, lambda: question.is_visible() or report.is_visible())
        if report.is_visible():
            break
        command = page.inner_text("#question-command")
        page.click("#question-actions button[data-action=approve]")
        approvals += 1
        print(f"     approved: {command}")
        wait_until(
            page,
            lambda c=command: (
                report.is_visible()
                or (question.is_visible() and page.inner_text("#question-command") != c)
            ),
        )
    expect(report).to_be_visible(timeout=WAIT_MS)
    title = page.inner_text("#report-title")
    check(title == "Setup verified", f"run ends with '{title}' after {approvals} approvals")
    level = page.inner_text("#outcome-verification")
    check("Checked by SetupPilot" in level, f"the report says who checked it: {level}")
    repo_folder = str(folder.resolve() / "sampleproject")
    check(repo_folder in page.inner_text("#outcome-message"), f"the outcome names {repo_folder}")
    check(page.locator("#cancel").is_hidden(), "Cancel is hidden once the run is over")
    steps = page.locator("#steps li").count()
    page.reload()
    expect(report).to_be_visible(timeout=10_000)
    check(page.locator("#steps li").count() == steps, f"a reload shows the same {steps} steps")


def check_cancel(page: Page, base: str, folder: Path) -> str:
    """Returns the cancelled run's id; its page is reused for the made-up run screens."""
    start_run(page, base, CANCEL_REPO, folder)
    expect(page.locator("#question")).to_be_visible(timeout=60_000)
    page.click("#cancel")
    expect(page.locator("#status")).to_have_attribute("data-status", "cancelled", timeout=30_000)
    check(page.locator("#question").is_hidden(), "cancel hides the open question")
    check(page.locator("#cancel").is_hidden(), "cancel hides the Cancel button")
    retry, repo_folder = page.locator("#outcome-retry"), str(folder.resolve() / "classnames")
    is_named = retry.is_visible() and repo_folder in retry.inner_text()
    check(is_named, f"a cancelled run names the folder to delete: {repo_folder}")
    return page.url.rsplit("/", 1)[-1]


def check_pages(page: Page, base: str) -> None:
    for path, text in [("/runs", "sampleproject"), ("/metrics", ""), ("/settings", "GitHub")]:
        response = page.goto(base + path)
        is_ok = response is not None and response.status == 200
        check(is_ok and text in page.inner_text("body"), f"{path} loads {text}".strip())


def check_chat(page: Page, base: str) -> None:
    page.goto(base + "/chat")
    has_key = page.locator("#ask-notice").count() == 0
    page.fill("#index-form input[name=repo_url]", SETUP_REPO)
    page.click("#index-form button[type=submit]")
    expect(page.locator(".message.note").last).to_contain_text("Indexed", timeout=WAIT_MS)
    if not has_key:
        is_explained = "Groq key" in page.inner_text("#ask-notice")
        check(is_explained and page.locator("#ask-form button").is_disabled(), "no key: Ask is off")
        return
    asked = [
        ("What is the capital of France?", "could not find"),
        ("What license does this project use?", "mit"),
    ]
    problems, answers = page.locator(".message.error"), page.locator(".message.bot")
    for number, (question, expected) in enumerate(asked, start=1):
        page.fill("#ask-form input[name=question]", question)
        page.click("#ask-form button")
        wait_until(page, lambda n=number: problems.count() > 0 or answers.count() >= n, 60_000)
        answer = (problems if problems.count() else answers).last.inner_text()
        check(expected in answer.lower(), f"{question!r} -> {answer.strip()[:120]!r}")
    # The model does not always write [1] into its text, so only the source list is required.
    links = page.locator(".citations a.chip")
    hrefs = [links.nth(i).get_attribute("href") or "" for i in range(links.count())]
    check(
        bool(hrefs) and all(h.startswith("https://github.com/") for h in hrefs),
        f"answers list file:line sources that link only to github.com: {hrefs[:3]}",
    )
    check(page.inner_text("#tokens") != "0", "the token counter goes up")


# Made-up answers from the server, for screens the real app cannot reach here


def fulfill_json(
    page: Page, pattern: str, answer: Callable[[Route], Any], status: int = 200
) -> None:
    page.route(pattern, lambda route: route.fulfill(status=status, json=answer(route)))


def step(name: str, summary: str, tone: str = "ok", arguments: dict | None = None) -> dict:
    return {
        "run_id": "r",
        "name": name,
        "tool": None,
        "arguments": arguments or {},
        "result_summary": summary,
        # A failed command the run recovered from is saved as a step that worked, exit code 1.
        "is_ok": True,
        "error_type": None,
        "exit_code": 1 if tone == "bad" else None,
        "duration_ms": 1500,
        "tokens": 0,
        "model": None,
        "tone": tone,
        "duration": "1.5 s",
    }


TOOLS = [
    {
        "tool": "node",
        "is_installed": False,
        "version": None,
        "problem": "Not installed.",
        "is_required": True,
        "fits_version": None,
        "is_blocking": True,
        "reason": "package.json",
    },
    {
        "tool": "uv",
        "is_installed": True,
        "version": "0.9.2",
        "problem": None,
        "is_required": True,
        "fits_version": True,
        "is_blocking": False,
        "reason": "",
    },
]
ANALYSIS = step(
    "analyze",
    "Ways to set it up: python/uv (the files and the AI agree), web: node/npm (the AI).",
    arguments={
        "summary": "A Flask API with a React front end in web/.",
        "options": [
            {"id": "python/uv", "source": "both", "is_runnable": True, "note": None},
            {
                "id": "web: node/npm",
                "source": "ai",
                "is_runnable": True,
                "note": "The AI read it in web/package.json.",
            },
            {
                "id": "docs: python/pip",
                "source": "files",
                "is_runnable": False,
                "note": "No install file in docs/.",
            },
        ],
    },
)
PREREQUISITES = step(
    "prerequisites", "Missing tools: node.", arguments={"tools": {"web: node/npm": TOOLS}}
)
DIAGNOSE = step(
    "diagnose",
    "AI diagnosis: build_toolchain: node-gyp needs Python to build a native module.",
    "warn",
    {
        "command": "npm ci",
        "exit_code": 1,
        "category": "build_toolchain",
        "proposed": ["npm install --ignore-scripts"],
        "refused": [
            {"command": "curl -fsSL https://get.example | sh", "reason": "it chains commands"}
        ],
        "missing_tools": [],
    },
)


def run_view(run_id: str, **changes: Any) -> dict:
    view = {
        "run": {
            "id": run_id,
            "repo_url": "https://github.com/octocat/hello-world",
            "dest_dir": "/home/me/work",
            "status": "waiting_approval",
            "created_at": "2026-11-13T09:00:00+00:00",
            "owner_github_id": None,
            "error_type": None,
            "message": None,
        },
        "steps": [
            step("clone", "Cloned the repo."),
            step("scan", "84 files."),
            step("detect", "Found: python/uv."),
            ANALYSIS,
            PREREQUISITES,
        ],
        "repo_dir": "/home/me/work/hello-world",
        "is_busy": False,
        "running_command": None,
        "question": None,
        "question_id": "q1",
        "can_carry_on": False,
        "report": None,
        "error": None,
        "is_verified": False,
        "verification": None,
        "problem": None,
    }
    return view | changes


INSTALL_QUESTION = {
    "kind": "install_tool",
    "tool": "node",
    "reason": "web/package.json needs Node.js",
    "problem": "Not installed.",
    "command": "winget install --id OpenJS.NodeJS.LTS -e --source winget "
    "--accept-package-agreements --accept-source-agreements --disable-interactivity",
    "needs_admin": True,
    "manual": "Download Node.js from https://nodejs.org/en/download and run it.",
    "note": "winget accepts the package's license terms for you. Windows may ask for admin "
    "rights in another window.",
    "os": "Windows",
    "last_install": None,
    "round": "1 of 3",
}
FIX_QUESTION = {
    "kind": "fix_by_hand",
    "command": "npm ci",
    "exit_code": 1,
    "diagnosis": {
        "category": "build_toolchain",
        "explanation": "node-gyp needs Python and a C++ compiler to build a native module.",
    },
    "user_action": "Install Visual Studio Build Tools with the C++ workload, then press I fixed "
    "it.",
    "refused": [{"command": "curl -fsSL https://get.example | sh", "reason": "it chains commands"}],
}
CHOICE_QUESTION = {
    "kind": "choose_stack",
    "options": [
        {
            "label": "python/uv",
            "evidence": ["pyproject.toml", "uv.lock"],
            "prereqs": TOOLS[1:],
            "project": ".",
            "source": "both",
            "note": None,
        },
        {
            "label": "web: node/npm",
            "evidence": ["web/package.json"],
            "prereqs": TOOLS[:1],
            "project": "web",
            "source": "ai",
            "note": "The AI read it in web/package.json.",
        },
    ],
    "conflicts": [],
}
APPROVAL = {
    "kind": "approve_command",
    "step": "1 of 3",
    "command": "uv venv .venv",
    "reason": "Make a virtual environment in the repo folder.",
    "is_check": False,
    "is_optional": False,
    "origin": "setuppilot",
    "diagnosis": None,
    "warning": "",
    "in_docker": False,
    "edit_error": None,
}
FIX_APPROVAL = {
    "kind": "approve_command",
    "step": "3 of 4",
    "command": "npm install --ignore-scripts",
    "reason": "A fix from the AI: skip the native build that failed.",
    "is_check": False,
    "is_optional": False,
    "origin": "fix",
    "diagnosis": {"category": "build_toolchain", "explanation": "node-gyp found no Python."},
    "warning": "",
    "in_docker": False,
    "edit_error": None,
}


def finished_ai_only(run_id: str) -> dict:
    run = run_view(run_id)["run"] | {"status": "failed", "error_type": "VerifyFailed"}
    plan = step("plan", "2 commands to approve.", arguments={"start_command": "npm run dev"})
    verify = step("verify", "2 of 2 checks passed. Checked only by the AI-proposed checks.")
    return run_view(
        run_id,
        run=run,
        question=None,
        question_id=None,
        report="Setup NOT verified by SetupPilot.",
        verification="ai_only",
        steps=[
            *run_view(run_id)["steps"],
            plan,
            step("execute", "npm ci: exit 1.", "bad"),
            DIAGNOSE,
            step("execute", "npm install --ignore-scripts: exit 0."),
            verify,
            step("report", "Wrote the report."),
        ],
    )


def finished_verified(run_id: str) -> dict:
    view = finished_ai_only(run_id)
    report = "Setup verified: it works.\n- uv venv .venv: ran, exit code 0"
    return view | {
        "run": view["run"] | {"status": "succeeded", "error_type": None},
        "report": report,
        "verification": "setuppilot",
        "is_verified": True,
    }


def serve_view(page: Page, view: dict, answers: list[Any] | None = None) -> None:
    body = f"data: {json.dumps(view)}\n\n"
    page.route(
        "**/api/runs/*/events",
        lambda route: route.fulfill(body=body, content_type="text/event-stream"),
    )

    def answered(route: Route) -> dict:
        if answers is not None:
            answers.append(route.request.post_data_json)
        return {"ok": True}

    fulfill_json(page, "**/api/runs/*/answer", answered)


GITHUB_ON = {
    "is_enabled": True,
    "turn_on": None,
    "is_signed_in": False,
    "is_this_browser": False,
    "login": None,
    "notice": None,
    "install_url": "https://github.com/apps/setuppilot-me/installations/new",
}
REPOS = [
    {
        "full_name": "octocat/hello-world",
        "url": "https://github.com/octocat/hello-world",
        "description": "My first repo on GitHub.",
        "visibility": "public",
        "language": "Python",
        "default_branch": "main",
        "is_archived": False,
        "is_fork": False,
        "pushed_at": "2026-11-10T08:00:00+00:00",
    },
    {
        "full_name": "octocat/secret-plans",
        "url": "https://github.com/octocat/secret-plans",
        "description": "Plans for a very long weekend, with a description long enough to need "
        "two lines on a phone screen.",
        "visibility": "private",
        "language": "TypeScript",
        "default_branch": "main",
        "is_archived": False,
        "is_fork": False,
        "pushed_at": "2026-11-12T08:00:00+00:00",
    },
    {
        "full_name": "octocat/old-fork",
        "url": "https://github.com/octocat/old-fork",
        "description": None,
        "visibility": "public",
        "language": None,
        "default_branch": "main",
        "is_archived": True,
        "is_fork": True,
        "pushed_at": None,
    },
]


@dataclass
class Screens:
    base: str
    run_id: str
    preflight: dict
    sent: list[dict] = field(default_factory=list)

    def common(self, page: Page) -> None:
        # The pre-flight asks GitHub; the screens reuse the one real answer instead.
        fulfill_json(page, "**/api/preflight*", lambda route: self.preflight)

    def signed_in(self, page: Page) -> None:
        page.add_init_script(f"localStorage.setItem('setuppilot-github-binding', '{KEY}')")
        status = GITHUB_ON | {"is_signed_in": True, "is_this_browser": True, "login": "octocat"}
        fulfill_json(page, "**/api/github/status", lambda route: status)
        fulfill_json(
            page,
            "**/api/github/repos",
            lambda route: {
                "repos": REPOS,
                "is_complete": True,
                "install_url": GITHUB_ON["install_url"],
            },
        )

    def setup_url(self, page: Page) -> None:
        page.goto(self.base + "/")
        expect(page.locator("#preflight li").first).to_be_visible()

    def setup_sign_in_off(self, page: Page) -> None:
        self.setup_url(page)
        page.click("label.filter:has-text('My GitHub repos')")
        expect(page.locator("#gh-off")).to_be_visible()

    def setup_waiting(self, page: Page) -> None:
        fulfill_json(page, "**/api/github/status", lambda route: GITHUB_ON)
        code = {
            "pending_id": "p1",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "warning": "Only type a code on github.com that this SetupPilot page showed you.",
        }
        fulfill_json(page, "**/api/github/signin", lambda route: code)
        fulfill_json(page, "**/api/github/signin/poll", lambda route: {"state": "pending"})
        self.setup_url(page)
        page.click("label.filter:has-text('My GitHub repos')")
        page.click("#gh-sign-in")
        expect(page.locator("#gh-waiting")).to_be_visible()

    def setup_signed_in(self, page: Page) -> None:
        self.signed_in(page)
        self.setup_url(page)
        expect(page.locator("#gh-repos li")).to_have_count(len(REPOS))

    def setup_browse(self, page: Page) -> None:
        fulfill_json(
            page,
            "**/api/pick-folder",
            lambda route: {"status": "picked", "path": "/home/me/work", "picker": "osascript"},
        )
        self.setup_url(page)
        if page.locator("#browse").count():
            page.click("#browse")
            expect(page.locator("#pick-note")).to_contain_text("Picked")

    def run(self, view: dict) -> Callable[[Page], None]:
        def draw(page: Page) -> None:
            serve_view(page, view)
            page.goto(f"{self.base}/runs/{self.run_id}")
            page.wait_for_timeout(1200)

        return draw

    def runs_page(self, page: Page) -> None:
        self.signed_in(page)
        listed = {
            "id": self.run_id,
            "repo_url": "https://github.com/octocat/secret-plans",
            "status": "succeeded",
            "created_at": "2026-11-13T23:00:00+00:00",
            "seconds": 95.0,
            "took": "1 min 35 s",
            "tokens": 3400,
            "error_type": None,
            "message": None,
        }
        fulfill_json(page, "**/api/github/runs", lambda route: {"runs": [listed]})
        page.goto(self.base + "/runs")
        expect(page.locator("table.runs tr", has_text="secret-plans")).to_be_visible()

    def chat_answers(self, page: Page) -> None:
        self.signed_in(page)
        fulfill_json(
            page,
            "**/api/chat/repos",
            lambda route: {
                "repos": ["octocat/secret-plans", "pypa/sampleproject"],
                "private": ["octocat/secret-plans"],
            },
        )
        replies = iter(
            [
                {
                    "status": "answered",
                    "text": "Run it with `uv run app.py` [1]. The tests use pytest [2].",
                    "citations": [
                        {
                            "n": 1,
                            "path": "README.md",
                            "start_line": 40,
                            "end_line": 52,
                            "url": "https://github.com/pypa/sampleproject/blob/621e497/README.md#L40-L52",
                            "quote": "uv run app.py",
                        },
                        {
                            "n": 2,
                            "path": "noxfile.py",
                            "start_line": 10,
                            "end_line": 10,
                            "url": "javascript:alert(1)",
                            "quote": "session.run('pytest')",
                        },
                    ],
                    "looked_at": [],
                    "tokens": 1200,
                },
                {
                    "status": "not_found",
                    "text": "I could not find this in the repo.",
                    "citations": [],
                    "looked_at": ["README.md", "docs/install.md"],
                    "tokens": 0,
                },
                {
                    "status": "small_talk",
                    "text": "Happy to help. Ask me anything about pypa/sampleproject.",
                    "citations": [],
                    "looked_at": [],
                    "tokens": 0,
                },
            ]
        )
        fulfill_json(page, "**/api/chat/ask", lambda route: next(replies))
        page.goto(self.base + "/chat")
        page.wait_for_timeout(500)
        if page.locator("#ask-notice").count():
            return
        for question in ("How do I run it?", "What is the capital of France?", "thanks"):
            page.fill("#ask-question", question)
            page.click("#ask-form button[type=submit]")
            page.wait_for_timeout(300)
        expect(page.locator(".message.bot")).to_have_count(3)

    def all(self) -> dict[str, Callable[[Page], None]]:
        view = run_view(self.run_id)
        return {
            "setup-url": self.setup_url,
            "setup-sign-in-off": self.setup_sign_in_off,
            "setup-sign-in-code": self.setup_waiting,
            "setup-signed-in-repos": self.setup_signed_in,
            "setup-browse-picked": self.setup_browse,
            "run-install-tool": self.run(view | {"question": INSTALL_QUESTION}),
            "run-fix-by-hand": self.run(
                view | {"question": FIX_QUESTION, "steps": [*view["steps"], DIAGNOSE]}
            ),
            "run-choose-sub-project": self.run(view | {"question": CHOICE_QUESTION}),
            "run-approve-fix": self.run(
                view | {"question": FIX_APPROVAL, "steps": [*view["steps"], DIAGNOSE]}
            ),
            "run-busy-installing": self.run(
                view
                | {
                    "is_busy": True,
                    "running_command": INSTALL_QUESTION["command"],
                    "question": None,
                    "run": view["run"] | {"status": "running"},
                    "steps": [*view["steps"], step("offer_install", "Install: node.")],
                }
            ),
            "run-ai-only-report": self.run(finished_ai_only(self.run_id)),
            "run-approve-command": self.run(view | {"question": APPROVAL}),
            "run-verified-report": self.run(finished_verified(self.run_id)),
            "runs-with-signed-in-run": self.runs_page,
            "chat-answers": self.chat_answers,
            "settings": lambda page: page.goto(self.base + "/settings"),
            "metrics": lambda page: page.goto(self.base + "/metrics"),
        }


def check_screen_behaviour(browser: Browser, screens: Screens) -> None:
    """What the made-up screens must do, beyond looking right."""
    context = browser.new_context()
    page = context.new_page()
    page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
    screens.common(page)
    sent: list[dict] = []

    def started(route: Route) -> dict:
        sent.append(
            {
                "body": route.request.post_data_json,
                "key": route.request.headers.get("x-setuppilot-binding"),
            }
        )
        return {"error": {"message": "Stopped by the browser check.", "details": ""}}

    fulfill_json(page, "**/api/runs", started, status=400)
    screens.setup_signed_in(page)
    page.locator("label.repo-option", has_text="secret-plans").click()
    page.fill("input[name=dest_dir]", "/home/me/work")
    page.click("#setup-form button[type=submit]")
    wait_until(page, lambda: bool(sent), 5000)
    check(
        bool(sent)
        and sent[0]["body"]["access"] == "github"
        and sent[0]["key"] == KEY
        and sent[0]["body"]["repo_url"] == "https://github.com/octocat/secret-plans",
        f"a picked private repo starts with access github and this browser's key: {sent[:1]}",
    )
    page.fill("#gh-search", "plans")
    page.wait_for_timeout(600)
    check(page.locator("#gh-repos li").count() == 1, "the repo search filters the list")
    fulfill_json(page, "**/api/github/signout", lambda route: {"warning": None})
    page.unroute("**/api/github/status")
    fulfill_json(page, "**/api/github/status", lambda route: GITHUB_ON)
    page.click("#gh-sign-out")
    expect(page.locator("#gh-sign-in")).to_be_visible()
    key = page.evaluate("localStorage.getItem('setuppilot-github-binding')")
    check(key is None, "sign-out forgets this browser's key")
    context.close()

    context = browser.new_context()
    page = context.new_page()
    screens.common(page)
    screens.signed_in(page)
    page.unroute("**/api/github/repos")
    ended = {
        "error": {"message": "This needs your GitHub sign-in, from this browser.", "details": ""}
    }
    fulfill_json(page, "**/api/github/repos", lambda route: ended, status=401)
    page.goto(screens.base + "/")
    page.wait_for_timeout(1500)
    check(
        page.evaluate("localStorage.getItem('setuppilot-github-binding')") is None,
        "a 401 to a request with the key forgets the key",
    )
    context.close()

    context = browser.new_context()
    page = context.new_page()
    answers: list[Any] = []
    view = run_view(screens.run_id) | {"question": INSTALL_QUESTION}
    serve_view(page, view, answers)
    page.goto(f"{screens.base}/runs/{screens.run_id}")
    expect(page.locator("#question")).to_be_visible()
    is_focused = page.evaluate("document.activeElement.id") == "question-title"
    check(is_focused, "a new question takes the focus, so a screen reader reads it")
    hint = page.inner_text("#keys-hint")
    check(all(k in hint for k in ("I install", "C check again", "S stop")), f"install keys: {hint}")
    page.wait_for_timeout(800)
    page.keyboard.press("c")
    wait_until(page, lambda: bool(answers), 3000)
    check(answers[:1] == [{"action": "recheck", "question_id": "q1"}], f"C checks again: {answers}")
    context.close()

    context = browser.new_context()
    page = context.new_page()
    serve_view(page, run_view(screens.run_id) | {"question": FIX_QUESTION})
    page.goto(f"{screens.base}/runs/{screens.run_id}")
    expect(page.locator("#question-diagnosis")).to_be_visible()
    text = page.inner_text("#question")
    check(
        "may be wrong" in text.lower() and "Visual Studio Build Tools" in text,
        "fix by hand shows the AI's guess as a guess, and what to do",
    )
    context.close()

    context = browser.new_context()
    page = context.new_page()
    serve_view(page, finished_ai_only(screens.run_id))
    page.goto(f"{screens.base}/runs/{screens.run_id}")
    expect(page.locator("#report-card")).to_be_visible()
    badge = page.locator("#verification-badge")
    check(
        badge.get_attribute("data-tone") == "warn" and "AI-proposed" in badge.inner_text(),
        "ai_only reads as weaker",
    )
    check(page.inner_text("#start-command") == "npm run dev", "the start command is the next step")
    tools = page.inner_text("#analysis")
    check(
        "node missing" in tools and "From the AI only" in tools,
        f"the analysis shows sources and tools: {tools[:200]!r}",
    )
    context.close()

    context = browser.new_context()
    page = context.new_page()
    screens.chat_answers(page)
    options = page.locator("#repo option").all_inner_texts()
    check("octocat/secret-plans (private)" in options, f"private chat repos are marked: {options}")
    if page.locator("#ask-notice").count():
        print("SKIP the made-up chat answers: the app has no Groq key, so Ask is off")
    else:
        chips = page.locator(".citations .chip")
        check(
            chips.nth(1).evaluate("node => node.tagName") == "SPAN",
            "a source link that is not github.com is not a link",
        )
        refs = page.locator(".cite-ref")
        check(
            refs.count() == 2 and refs.first.get_attribute("href") == "#a1-s1",
            "[1] in an answer links to its source below the answer",
        )
        check(
            "I looked in:" in page.locator(".message.bot.not_found").inner_text(),
            "not found says where it looked",
        )
    context.close()


def check_screens(browser: Browser, screens: Screens, shots: Path) -> None:
    """Each screen, in every variant: axe finds nothing, nothing scrolls sideways, and a
    screenshot is saved. bypass_csp only lets axe in; the app's own checks run without it."""
    shots.mkdir(parents=True, exist_ok=True)
    for name, draw in screens.all().items():
        for variant, (scheme, width, height) in VARIANTS.items():
            context = browser.new_context(
                viewport={"width": width, "height": height}, color_scheme=scheme, bypass_csp=True
            )
            page = context.new_page()
            page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
            screens.common(page)
            draw(page)
            page.add_script_tag(url=AXE_URL)
            found = page.evaluate(AXE_PROBLEMS)
            check(not found, f"axe: {name} {variant} " + ("; ".join(found) or "no problems"))
            page.screenshot(path=str(shots / f"{name}-{variant}.png"), full_page=True)
            if variant == "light-desktop":
                for wide in WIDTHS:
                    page.set_viewport_size({"width": wide, "height": height})
                    page.wait_for_timeout(200)
                    scroll = page.evaluate("document.documentElement.scrollWidth")
                    check(
                        scroll <= wide,
                        f"{name} fits {wide} px without scrolling sideways ({scroll})",
                    )
            context.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5005")
    parser.add_argument("--work", type=Path, required=True, help="An empty folder for the clones.")
    default_shots = os.environ.get("SETUPPILOT_UI_SHOTS") or Path(tempfile.gettempdir()) / "ui-v2"
    parser.add_argument("--shots", type=Path, default=default_shots, help="Where screenshots go.")
    parser.add_argument(
        "--screens-only",
        metavar="RUN_ID",
        help="Only the made-up screens, drawn on this run's page: no clones, no AI calls.",
    )
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    if any(args.work.iterdir()):
        raise SystemExit(f"{args.work} is not empty. Pick an empty folder for the clones.")
    # "chrome" uses the Chrome you already have; "chromium" needs `playwright install chromium`.
    channel = os.environ.get("SETUPPILOT_E2E_BROWSER", "chrome")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        page = browser.new_page()
        page.on("console", lambda m: m.type == "error" and print(f"     console: {m.text}"))
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
        page.on("dialog", lambda d: d.accept())
        page.goto(args.base + "/")
        expect(page.locator("#preflight li")).to_have_count(4, timeout=20_000)
        check(True, "pre-flight: " + " | ".join(page.locator("#preflight li").all_inner_texts()))
        token = page.locator('meta[name="csrf-token"]').get_attribute("content") or ""
        preflight = page.request.get(f"{args.base}/api/preflight", headers={"X-CSRF-Token": token})
        run_id = args.screens_only
        if run_id is None:
            check_setup_page(page, args.base)
            check_setup_run(page, args.base, args.work)
            run_id = check_cancel(page, args.base, args.work)
            check_pages(page, args.base)
            check_chat(page, args.base)
        screens = Screens(args.base, run_id, preflight.json())
        check_screen_behaviour(browser, screens)
        check_screens(browser, screens, args.shots)
        browser.close()
    print(f"\nScreenshots: {args.shots}")
    print("\nFailed:\n  " + "\n  ".join(problems) if problems else "\nAll browser checks passed.")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
