"""Click through the web app in a real browser. test.sh starts the app and runs this file.

Run by hand: start `uv run setuppilot web`, then
uv run --with playwright python scripts/browser_check.py --work ~/setuppilot-test/browser
"""

import argparse
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

SETUP_REPO = "https://github.com/pypa/sampleproject"
CANCEL_REPO = "https://github.com/JedWatson/classnames"
WAIT_MS = 300_000
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


def check_setup_run(page: Page, base: str, folder: Path) -> None:
    start_run(page, base, SETUP_REPO, folder)
    question, report = page.locator("#question"), page.locator("#report-card")
    expect(question).to_be_visible(timeout=60_000)

    page.click("#question-actions button[data-action=edit]")
    page.fill("#edit-command", "rm -rf /")
    page.click("#edit-send")
    expect(page.locator("#question-edit-error")).to_be_visible(timeout=30_000)
    check("blocked" in page.inner_text("#question-edit-error"), "a dangerous edit is refused")

    approvals = 0
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
    check(title == "Report: setup verified", f"run ends with '{title}' after {approvals} approvals")
    check(page.locator("#cancel").is_hidden(), "Cancel is hidden once the run is over")
    steps = page.locator("#steps li").count()
    page.reload()
    expect(report).to_be_visible(timeout=10_000)
    check(page.locator("#steps li").count() == steps, f"a reload shows the same {steps} steps")


def check_cancel(page: Page, base: str, folder: Path) -> None:
    start_run(page, base, CANCEL_REPO, folder)
    expect(page.locator("#question")).to_be_visible(timeout=60_000)
    page.click("#cancel")
    expect(page.locator("#status")).to_have_text("cancelled", timeout=30_000)
    check(page.locator("#question").is_hidden(), "cancel hides the open question")
    check(page.locator("#cancel").is_hidden(), "cancel hides the Cancel button")


def check_pages(page: Page, base: str) -> None:
    for path, text in [("/runs", "sampleproject"), ("/metrics", ""), ("/settings", "")]:
        response = page.goto(base + path)
        is_ok = response is not None and response.status == 200
        check(is_ok and text in page.inner_text("body"), f"{path} loads {text}".strip())


def check_chat(page: Page, base: str) -> None:
    page.goto(base + "/chat")
    has_key = page.locator(".banner.warn").count() == 0
    page.fill("#index-form input[name=repo_url]", SETUP_REPO)
    page.click("#index-form button")
    expect(page.locator(".message.bot").last).to_contain_text("Indexed", timeout=WAIT_MS)
    asked = [
        ("What is the capital of France?", "could not find"),
        ("What license does this project use?", "mit"),
    ]
    error, answers = page.locator("#error"), page.locator(".message.bot")
    for number, (question, expected) in enumerate(asked, start=2):
        page.fill("#ask-form input[name=question]", question)
        page.click("#ask-form button")
        wait_until(page, lambda n=number: error.is_visible() or answers.count() >= n, 60_000)
        if not has_key:
            is_refused = error.is_visible() and "Groq key" in error.inner_text()
            check(is_refused, f"no key: {question!r} says a key is needed")
            continue
        answer = answers.last.inner_text()
        check(expected in answer.lower(), f"{question!r} -> {answer.strip()[:120]!r}")
    if has_key:
        check(page.locator(".chip").count() > 0, "answers show their source files")
        check(page.inner_text("#tokens") != "0", "the token counter goes up")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5005")
    parser.add_argument("--work", type=Path, required=True, help="An empty folder for the clones.")
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
        check_setup_run(page, args.base, args.work)
        check_cancel(page, args.base, args.work)
        check_pages(page, args.base)
        check_chat(page, args.base)
        browser.close()
    print("\nFailed:\n  " + "\n  ".join(problems) if problems else "\nAll browser checks passed.")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
