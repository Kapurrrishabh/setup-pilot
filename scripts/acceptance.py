"""The checks that could not be run on the laptop SetupPilot was built on: real Groq and
Supabase, other machines (Windows first), Docker Desktop and other browsers. See "Testing still
to do" in README.md. test.ps1 and test.sh only run this file.

    uv run python scripts/acceptance.py                 run every part this machine can run
    uv run python scripts/acceptance.py groq supabase   run only these parts

Parts: basics groq supabase setup docker chat browser
Results go to test-results/<time>-<machine>/. Send that folder to the team; it has no secrets.
"""

import contextlib
import io
import ntpath
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import psycopg
import yaml
from dotenv import dotenv_values

from setuppilot.config import DatabaseConfig, Settings, load_settings
from setuppilot.db.postgres import open_pool
from setuppilot.errors import DatabaseUnavailable, SetupPilotError
from setuppilot.evaluation.chat_eval import load_chat_cases
from setuppilot.evaluation.setup_eval import load_setup_cases
from setuppilot.guardrails.commands import ExecTarget
from setuppilot.guardrails.docker_mode import DOCKER_IMAGES, RUNS_ON_HOST
from setuppilot.guardrails.process_tree import start_tree
from setuppilot.guardrails.runner import HOST_TARGET, child_env, find_program, launch_args
from setuppilot.redact import redact
from setuppilot.tools.compose import COMPOSE_NAMES

SETUP_REPOS = Path("eval/setup_repos.yaml")
CHAT_QUESTIONS = Path("eval/chat_questions.yaml")
SAMPLE_REPO = "https://github.com/pypa/sampleproject"
# The spec's target for chatbot grounding: answers (and searches) that use a right file.
GROUNDED_TARGET_PERCENT = 80
GUARDRAIL_COVERAGE = "guardrail coverage at least 90%"
# PROVISIONAL: the minute test.sh waited for the first start; not timed on Windows yet.
_WEB_START_SECONDS = 60
_WEB_TRY_SECONDS = 5
# Into a pipe, Python holds output back, and on Windows writes the ANSI code page and stops at
# any other character. So our tools write UTF-8, at once, as they would to a console.
_PIPE_OUTPUT = {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
# Where Playwright's "chrome" channel looks (playwright 1.64), so this agrees with
# browser_check.py. On Windows it looks in each of a few folders.
_CHROME = {
    "linux": "/opt/google/chrome/chrome",
    "darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
}
_CHROME_IN_WINDOWS_FOLDER = r"Google\Chrome\Application\chrome.exe"
_DONE_LINE = re.compile(r"^(\d+) of (\d+) [a-z]+ done\.$", re.MULTILINE)
_COUNT = re.compile(r"(\d+)/(\d+) \(\d+%\)")
_RESUME_HINT = re.compile(r"setuppilot resume ([0-9a-f-]+)")
_TABLES_WITHOUT_ROW_SECURITY = (
    "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
    "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity"
)
_GRANTS_TO_API_ROLES = (
    "select distinct table_name, grantee from information_schema.role_table_grants "
    "where table_schema = 'public' and grantee in ('anon', 'authenticated')"
)


@dataclass(frozen=True)
class Verdict:
    """Whether a result table passes, and why, in words for the summary."""

    is_pass: bool
    reason: str


@dataclass(frozen=True)
class Check:
    """A command that passes when it exits with 0."""

    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)


def chosen_parts(args: Sequence[str]) -> list[str]:
    """The parts to run, always in the order of PARTS; none named means all of them."""
    if unknown := [arg for arg in args if arg not in PARTS]:
        raise ValueError(f"Unknown part: {' '.join(unknown)}. Pick from: {' '.join(PARTS)}")
    return [part for part in PARTS if not args or part in args]


def results_folder_name(stamp: str, hostname: str) -> str:
    """<time>-<machine>, with the short machine name made safe for a folder name."""
    return f"{stamp}-{re.sub(r'[^A-Za-z0-9-]', '-', hostname.split('.')[0])}"


def app_setting(name: str, environ: Mapping[str, str], dotenv: Mapping[str, str | None]) -> str:
    """The value the app will see for a setting like GROQ_API_KEY, or "". As in load_dotenv, a
    variable that is set, even to nothing, wins over .env."""
    return (environ[name] if name in environ else dotenv.get(name)) or ""


def basics_checks(host: ExecTarget) -> list[Check]:
    """The checks CI runs. Types are checked for every system on any machine, because mypy
    skips the code written for the other systems."""
    checks = [
        Check("install from the lock file", ("uv", "sync", "--locked")),
        Check("lint", ("uv", "run", "ruff", "check", ".")),
        Check("format", ("uv", "run", "ruff", "format", "--check", ".")),
        *(
            Check(f"types for {system}", ("uv", "run", "mypy", "--platform", name))
            for name, system in (("linux", "Linux"), ("win32", "Windows"), ("darwin", "macOS"))
        ),
    ]
    if host == "windows":
        # As in the Windows CI job: a text file opened without an encoding fails its test.
        tests = Check("unit tests", ("uv", "run", "pytest"), {"PYTHONWARNDEFAULTENCODING": "1"})
        return [*checks, tests]
    coverage = ("--include=src/setuppilot/guardrails/*", "--fail-under=90")
    return [
        *checks,
        Check("unit tests", ("uv", "run", "pytest", "--cov")),
        Check(GUARDRAIL_COVERAGE, ("uv", "run", "coverage", "report", *coverage)),
    ]


def newest_table(names: Iterable[str], kind: str) -> str | None:
    """The newest result table of one kind ("setup", "chat-full" and so on). Its name ends in
    the time it was written, so it sorts last; the digit keeps "setup" from "setup-docker"."""
    pattern = re.compile(rf"{re.escape(kind)}-\d[\d-]*\.md")
    return max((name for name in names if pattern.fullmatch(name)), default=None)


def judge_complete(table: str) -> Verdict:
    """An evaluation that stopped early (a limit, an error) writes "12 of 30 questions done". Its
    scores cover only those 12, so they must not count as a pass."""
    match = _DONE_LINE.search(table)
    if match is None:
        return Verdict(False, "the table has no 'N of N ... done' line")
    done, total = int(match[1]), int(match[2])
    if total == 0 or done != total:
        return Verdict(False, f"only {done} of {total} done, so its scores do not count")
    return Verdict(True, f"{done} of {total} done")


def judge_all_verified(table: str) -> Verdict:
    """A setup table passes when it is complete and every repo was verified."""
    if not (complete := judge_complete(table)).is_pass:
        return complete
    verified, total = _count(_total_cell(table, "Verified"))
    return Verdict(verified == total, f"{verified} of {total} verified")


def judge_grounded(table: str) -> Verdict:
    """A chat table passes when it is complete and at least GROUNDED_TARGET_PERCENT of the
    answerable questions used a right file."""
    if not (complete := judge_complete(table)).is_pass:
        return complete
    cell = _total_cell(table, "Grounded")
    if cell == "-":
        return Verdict(False, "no answerable question was scored")
    grounded, scored = _count(cell)
    is_pass = grounded * 100 >= scored * GROUNDED_TARGET_PERCENT
    return Verdict(is_pass, f"{grounded} of {scored} grounded, target {GROUNDED_TARGET_PERCENT}%")


def _total_cell(table: str, column: str) -> str:
    """One cell of the Total row in the file's first table."""
    rows: list[list[str]] = []
    for line in table.splitlines():
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
        elif rows:
            break
    if not rows or column not in rows[0]:
        raise ValueError(f"the table has no {column} column")
    total = next((row for row in rows if row[0].startswith("**Total")), None)
    if total is None or len(total) != len(rows[0]):
        raise ValueError("the table has no Total row that fits its columns")
    return total[rows[0].index(column)]


def _count(cell: str) -> tuple[int, int]:
    """'14/15 (93%)' gives (14, 15)."""
    if match := _COUNT.fullmatch(cell):
        return int(match[1]), int(match[2])
    raise ValueError(f"{cell!r} is not a count like 14/15 (93%)")


def docker_mode_repos(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The eval repos Docker mode can set up: their package manager has an image, or they are
    Compose repos, which run docker compose on this machine."""
    return [
        repo for repo in repos if repo["expected_manager"] in DOCKER_IMAGES.keys() | RUNS_ON_HOST
    ]


def paused_run_id(output: str) -> str | None:
    """The run id from the "Continue later with: setuppilot resume <id>" line."""
    ids = _RESUME_HINT.findall(output)
    return ids[-1] if ids else None


def chrome_paths(system: str, environ: Mapping[str, str]) -> list[str]:
    """Where Playwright looks for Google Chrome on this system (sys.platform)."""
    if system != "win32":
        return [_CHROME[system]] if system in _CHROME else []
    folders = [environ.get(name) for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")]
    if drive := environ.get("HOMEDRIVE"):
        folders += [drive + r"\Program Files", drive + r"\Program Files (x86)"]
    return [ntpath.join(folder, _CHROME_IN_WINDOWS_FOLDER) for folder in folders if folder]


def compose_files(clones: Path) -> list[Path]:
    """The Compose files at the top of each repo the evaluations cloned, which sit at
    clones/<part>/<owner>__<name>/<name>/."""
    return sorted(
        file
        for depth in range(4)
        for name in COMPOSE_NAMES
        for file in clones.glob("*/" * depth + name)
    )


def row_security_problems(url: str, database: DatabaseConfig) -> list[str]:
    """Tables without row level security, and grants to Supabase's API roles. It goes through
    the app's own pool, because psycopg's errors repeat the URL, password included."""
    pool = open_pool(url, database)
    try:
        with pool.connection() as conn:
            open_tables = conn.execute(_TABLES_WITHOUT_ROW_SECURITY).fetchall()
            grants = conn.execute(_GRANTS_TO_API_ROLES).fetchall()
    finally:
        pool.close()
    return [f"no row level security on {row['relname']}" for row in open_tables] + [
        f"{row['grantee']} has a grant on {row['table_name']}" for row in grants
    ]


class Report:
    """Shows each line and keeps it in test.log, with secrets hidden, and collects the summary."""

    def __init__(self, log: IO[str]) -> None:
        self._log = log
        self.summary: list[str] = []

    def say(self, text: str) -> None:
        text = redact(text)
        print(text, flush=True)
        self._log.write(text + "\n")
        self._log.flush()

    def outcome(self, name: str, is_pass: bool, why: str = "") -> None:
        self._note(f"{'PASS' if is_pass else 'FAIL'}  {name}" + (f" ({why})" if why else ""))

    def skipped(self, name: str, why: str) -> None:
        self._note(f"SKIP  {name} ({why})")

    def info(self, text: str) -> None:
        self._note(f"INFO  {text}")

    @property
    def has_failed(self) -> bool:
        return any(line.startswith("FAIL") for line in self.summary)

    def _note(self, line: str) -> None:
        self.summary.append(redact(line))
        self.say(line)


@dataclass(frozen=True)
class Acceptance:
    """One run of this script: the app's settings, and where its results and clones go."""

    report: Report
    settings: Settings
    results: Path
    clones: Path
    dotenv: Mapping[str, str | None]

    def setting(self, name: str) -> str:
        return app_setting(name, os.environ, self.dotenv)

    def run(
        self, argv: Sequence[str], stdin_text: str = "", extra_env: Mapping[str, str] | None = None
    ) -> tuple[int, str]:
        """Runs one of our own tools, showing and logging its output as it comes. Gives back its
        exit code and output."""
        self.report.say(f"$ {' '.join(argv)}")
        env = _tool_env() | dict(extra_env or {})
        args, executable = launch_args(_tool(argv[0]), argv[1:], env, HOST_TARGET)
        lines: list[str] = []
        with tempfile.TemporaryFile() as stdin:
            stdin.write(stdin_text.encode())
            stdin.seek(0)
            with subprocess.Popen(  # noqa: S603 - our own tools, no shell
                args,
                executable=executable,
                env=env,
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as process:
                for line in _lines(process.stdout):
                    lines.append(line)
                    self.report.say(line)
        return process.returncode, "\n".join(lines)

    def check(
        self, name: str, argv: Sequence[str], stdin_text: str = "",
        extra_env: Mapping[str, str] | None = None,
    ) -> None:  # fmt: skip
        self.report.say(f"\n== {name}")
        code, _ = self.run(argv, stdin_text, extra_env)
        self.report.outcome(name, code == 0, "" if code == 0 else f"exit code {code}")

    def evaluate(
        self, title: str, argv: Sequence[str], folder: Path, name: str, kind: str,
        judge: Callable[[str], Verdict],
    ) -> None:  # fmt: skip
        """Runs an evaluation that saves into folder, then judges its newest table of kind."""
        self.report.say(f"\n== {title}")
        code, _ = self.run([*argv, "--results", str(folder)])
        if code != 0:
            self.report.outcome(name, False, f"the evaluation stopped with exit code {code}")
            return
        found = newest_table((path.name for path in folder.glob("*.md")), kind)
        if found is None:
            self.report.outcome(name, False, f"no {kind} result table in {folder}")
            return
        try:
            verdict = judge((folder / found).read_text(encoding="utf-8"))
        except ValueError as error:
            verdict = Verdict(False, f"{folder / found}: {error}")
        self.report.outcome(name, verdict.is_pass, verdict.reason)


def _find_tool(name: str) -> str | None:
    """Where one of our own tools is, found the way the app finds a command's program."""
    return find_program(name, child_env(os.environ, HOST_TARGET))


def _tool(name: str) -> str:
    if (found := _find_tool(name)) is None:
        raise FileNotFoundError(f"{name} is not installed or not on PATH. Install it, then retry.")
    return found


def _tool_env() -> dict[str, str]:
    """Our own tools get the whole environment: they are the app and its tests, not a repo's
    install scripts."""
    return {**os.environ, **_PIPE_OUTPUT}


def _lines(stream: IO[bytes] | None) -> Iterator[str]:
    """A child's output, line by line, as text whatever bytes it holds."""
    if stream is None:
        raise ValueError("The child was started without stdout=PIPE.")
    for raw in stream:
        yield raw.decode("utf-8", errors="replace").rstrip("\r\n")


def _succeeds(argv: Sequence[str]) -> bool:
    """Runs one of our own tools without showing its output."""
    env = _tool_env()
    args, executable = launch_args(_tool(argv[0]), argv[1:], env, HOST_TARGET)
    return (
        subprocess.run(  # noqa: S603 - our own tools, no shell
            args,
            executable=executable,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _is_listening(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        return False
    return True


def _answers(process: subprocess.Popen[bytes], url: str) -> bool:
    """Waits until the web app answers, but not once it has ended."""
    # No proxy: one set in the environment would get the request instead of this machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + _WEB_START_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=_WEB_TRY_SECONDS):
                return True
        except OSError:
            time.sleep(1)
    return False


@contextlib.contextmanager
def web_app(acc: Acceptance, name: str, log_name: str) -> Iterator[str | None]:
    """Runs the web app as a child during the with block, then stops its whole process tree (a
    Job Object on Windows), so no server is left holding the port. Gives its address, or None
    after a FAIL for name when it did not start."""
    web = acc.settings.web
    if _is_listening(web.host, web.port):
        acc.report.outcome(name, False, f"something is already using port {web.port}; stop it")
        yield None
        return
    env, log_path = _tool_env(), acc.results / log_name
    args, executable = launch_args(_tool("uv"), ["run", "setuppilot", "web"], env, HOST_TARGET)
    process, tree = start_tree(args, Path.cwd(), env, executable, stderr=subprocess.STDOUT)
    with process, log_path.open("w", encoding="utf-8") as log:
        lines = (redact(line) + "\n" for line in _lines(process.stdout))
        copier = threading.Thread(target=log.writelines, args=(lines,), daemon=True)
        copier.start()
        try:
            base = f"http://{web.host}:{web.port}"
            if _answers(process, base + "/"):
                yield base
            else:
                acc.report.outcome(name, False, f"the web app did not start; see {log_path}")
                yield None
        finally:
            tree.close()
            copier.join()


def run_basics(acc: Acceptance) -> None:
    if not os.environ.get("TEST_DATABASE_URL"):
        acc.report.info(
            "TEST_DATABASE_URL is not set, so database tests are skipped (CI runs them)"
        )
    for check in basics_checks(HOST_TARGET):
        acc.check(check.name, check.argv, extra_env=check.env)
    if HOST_TARGET == "windows":
        acc.report.skipped(GUARDRAIL_COVERAGE, "measured on macOS and Linux, as in CI")


def run_groq(acc: Acceptance) -> None:
    if not acc.setting("GROQ_API_KEY"):
        acc.report.skipped("groq", "add GROQ_API_KEY to .env")
        return
    acc.check(
        "one real Groq call", ["uv", "run", "setuppilot", "ask", "Reply with one word: ready"]
    )


def run_supabase(acc: Acceptance) -> None:
    url = acc.setting("SUPABASE_DB_URL")
    if not url:
        acc.report.skipped(
            "supabase", "add SUPABASE_DB_URL to .env (Session pooler string, see README)"
        )
        return
    acc.check("Supabase write and read back", ["uv", "run", "setuppilot", "db-check"])
    _check_row_security(acc, url)
    # Stop at the first question, then carry on in a new process: the run must come back from
    # Supabase.
    acc.report.say("\n== pause a run, then resume it from a new process")
    folder = acc.clones / "resume"
    folder.mkdir(parents=True, exist_ok=True)
    _, paused = acc.run(["uv", "run", "setuppilot", "run", SAMPLE_REPO, str(folder)], "q\n")
    run_id = paused_run_id(paused)
    if run_id is None:
        acc.report.outcome("pause a run", False, "no 'setuppilot resume <id>' line above")
    else:
        # More approvals than the run asks for; it never reads the rest.
        resume = ["uv", "run", "setuppilot", "resume", run_id]
        acc.check("resume the paused run and verify it", resume, stdin_text="a\n" * 20)
    acc.check("list runs from Supabase", ["uv", "run", "setuppilot", "runs"])


def _check_row_security(acc: Acceptance, url: str) -> None:
    name = "row level security on every table, nothing granted to anon"
    acc.report.say(f"\n== {name}")
    try:
        problems = row_security_problems(url, acc.settings.database)
    except DatabaseUnavailable as error:
        acc.report.outcome(name, False, f"{error.message} {error.details}")
        return
    except psycopg.Error as error:
        # The database refused a query: fail this check and go on with the others.
        acc.report.outcome(name, False, f"the check query failed: {error}")
        return
    for problem in problems:
        acc.report.say(problem)
    acc.report.outcome(name, not problems, "see the problems above" if problems else "")


def run_setup(acc: Acceptance) -> None:
    acc.report.info(
        "repos need their tools: uv, node+npm, pnpm, poetry, go, cargo, ruby+bundler, java+maven, "
        "docker. A missing tool shows as PrereqMissing."
    )
    acc.evaluate(
        f"setup evaluation: {len(load_setup_cases(SETUP_REPOS))} real repos, on this machine "
        "(a few minutes)",
        ["uv", "run", "setuppilot", "eval-setup", "--repos", str(SETUP_REPOS),
         "--workspace", str(acc.clones / "eval")],
        acc.results / "setup", "every eval repo verified", "setup", judge_all_verified,
    )  # fmt: skip


def run_docker(acc: Acceptance) -> None:
    docker = _find_tool("docker")
    if docker is None or not _succeeds([docker, "info"]):
        acc.report.skipped("docker", "start Docker Desktop (or Colima on macOS) first")
        return
    repos = docker_mode_repos(yaml.safe_load(SETUP_REPOS.read_text(encoding="utf-8"))["repos"])
    repos_file = acc.results / "docker-repos.yaml"
    repos_file.write_text(yaml.safe_dump({"repos": repos}, sort_keys=False), encoding="utf-8")
    names = ", ".join(repo["url"].rsplit("/", 1)[1] for repo in repos)
    acc.evaluate(
        f"Docker mode: the {len(repos)} eval repos whose package manager has a Docker image "
        f"({names})",
        ["uv", "run", "setuppilot", "eval-setup", "--docker", "--repos", str(repos_file),
         "--workspace", str(acc.clones / "docker")],
        acc.results / "docker", "every Docker-mode repo verified", "setup-docker",
        judge_all_verified,
    )  # fmt: skip
    acc.report.say("\n== can a container reach the web app? (known problem, see docs/SECURITY.md)")
    with web_app(acc, "web app for the container check", "web-docker.log") as base:
        if base is None:
            return
        host, port = acc.settings.web.host, acc.settings.web.port
        probe = (
            "import urllib.request as u; u.urlopen(u.Request("
            f"'http://host.docker.internal:{port}/', headers={{'Host': '{host}:{port}'}}), "
            "timeout=5)"
        )
        # The Python image the eval above already pulled, so this needs no extra download.
        image = DOCKER_IMAGES["pip"]
        add_host = ("--add-host", "host.docker.internal:host-gateway")
        code, _ = acc.run(["docker", "run", "--rm", *add_host, image, "python", "-c", probe])
        if code == 0:
            acc.report.info(
                "a container CAN reach the web app on this machine (expected; close the app "
                "during --docker runs)"
            )
        else:
            acc.report.info(
                "a container could NOT reach the web app on this machine (or docker run failed; "
                "see above)"
            )


def stop_compose_apps(acc: Acceptance) -> None:
    """The Compose repos leave their containers running; stop them so a test run leaves
    nothing behind."""
    if _find_tool("docker") is None:
        return
    for file in compose_files(acc.clones):
        down = ["docker", "compose", "-f", str(file), "down", "--volumes", "--remove-orphans"]
        if _succeeds(down):
            acc.report.say(f"Stopped the containers of {file}")


def _eval_chat(mode: str) -> list[str]:
    questions = str(CHAT_QUESTIONS)
    return ["uv", "run", "setuppilot", "eval-chat", "--mode", mode, "--questions", questions]


def run_chat(acc: Acceptance) -> None:
    folder, target = acc.results / "chat", f"{GROUNDED_TARGET_PERCENT}%+"
    acc.evaluate(
        "chatbot search only (no LLM)", _eval_chat("retrieval"), folder,
        f"chatbot search finds the right file for {target}", "chat-retrieval", judge_grounded,
    )  # fmt: skip
    if not acc.setting("GROQ_API_KEY"):
        acc.report.skipped("chat full answers", "add GROQ_API_KEY to .env")
        return
    cases = load_chat_cases(CHAT_QUESTIONS)
    earlier = sum(1 for case in cases if case.follows)
    acc.evaluate(
        f"chatbot full answers ({len(cases)} questions, plus the {earlier} earlier questions the "
        "follow-ups ask first, real Groq)",
        _eval_chat("full"), folder, f"chatbot answers grounded for {target}", "chat-full",
        judge_grounded,
    )  # fmt: skip
    if not acc.setting("SUPABASE_DB_URL"):
        acc.report.skipped("chat replay", "the answer cache only survives a restart in Supabase")
        return
    acc.evaluate(
        "chatbot replay from cached answers (no new LLM calls)", _eval_chat("replay"), folder,
        "every answer came back from the cache", "chat-replay", judge_complete,
    )  # fmt: skip


def run_browser(acc: Acceptance) -> None:
    has_chrome = any(os.path.exists(path) for path in chrome_paths(sys.platform, os.environ))
    if not os.environ.get("SETUPPILOT_E2E_BROWSER") and not has_chrome:
        acc.report.skipped(
            "browser",
            "install Google Chrome, or run: uv run --group e2e playwright install chromium, then "
            "set SETUPPILOT_E2E_BROWSER=chromium and run this part again",
        )
        return
    with web_app(acc, "browser", "web.log") as base:
        if base is not None:
            acc.check(
                "click through the web app in a browser",
                ["uv", "run", "--group", "e2e", "python", "scripts/browser_check.py",
                 "--base", base, "--work", str(acc.clones / "browser")],
            )  # fmt: skip
    acc.report.info("also click through it by hand in Firefox, and Safari on a Mac (see README)")


# The order here is the order the parts run in.
PARTS: dict[str, Callable[[Acceptance], None]] = {
    "basics": run_basics,
    "groq": run_groq,
    "supabase": run_supabase,
    "setup": run_setup,
    "docker": run_docker,
    "chat": run_chat,
    "browser": run_browser,
}


def main(args: Sequence[str]) -> int:
    os.chdir(Path(__file__).resolve().parents[1])
    try:
        parts = chosen_parts(args)
        settings = load_settings(Path("config.yaml"))
    except ValueError as error:
        print(error)
        return 2
    except SetupPilotError as error:
        print(f"{error.message}\nDetails: {error.details}")
        return 2
    if _find_tool("git") is None:
        print("git is missing. Install git first.")
        return 2
    if isinstance(sys.stdout, io.TextIOWrapper):
        # A Windows console shows any character, but a file or pipe gets the ANSI code page.
        sys.stdout.reconfigure(errors="replace")
    stamp, hostname = datetime.now(UTC).strftime("%Y%m%d-%H%M%S"), socket.gethostname()
    results = Path("test-results") / results_folder_name(stamp, hostname)
    # Not under Desktop, Documents or Downloads: Docker on macOS cannot read them, and Windows
    # often syncs them to OneDrive, which can lock files while an install writes them.
    clones = Path(os.environ.get("SETUPPILOT_TEST_WORK") or Path.home() / "setuppilot-test" / stamp)
    results.mkdir(parents=True)
    with (results / "test.log").open("w", encoding="utf-8") as log:
        report = Report(log)
        dotenv = dotenv_values(".env", encoding="utf-8-sig")
        acc = Acceptance(report, settings, results, clones, dotenv)
        system = f"{platform.platform()} ({platform.machine()}), Python {platform.python_version()}"
        report.info(f"machine: {system}, {hostname}; parts: {' '.join(parts)}")
        report.info(f"results: {results} ; clones: {clones}")
        for part in parts:
            PARTS[part](acc)
        stop_compose_apps(acc)
        report.say("\n======== summary ========")
        for line in report.summary:
            report.say(line)
        (results / "summary.txt").write_text("\n".join(report.summary) + "\n", encoding="utf-8")
        if clones.exists():
            report.say(
                f"Clones are in {clones}. Delete it when done (on macOS and Linux, Go's read-only "
                "files may need chmod -R u+w first)."
            )
    return 1 if report.has_failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
