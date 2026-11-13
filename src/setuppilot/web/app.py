"""The Flask web app: setup runs with live progress and approvals, the chatbot, history, metrics."""

import json
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from flask import Flask, Response, abort, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from setuppilot.agent.driver import ANSWERS_BY_KIND
from setuppilot.chat.bot import MAX_QUESTION_CHARS, ChatSession, RepoChatbot
from setuppilot.chat.embed import Embedder
from setuppilot.chat.repos import index_for_chat
from setuppilot.config import Settings
from setuppilot.db.reports import HISTORY_LIMIT, RunRow, metrics_summary, recent_runs
from setuppilot.db.runs import no_such_run
from setuppilot.db.stores import Stores
from setuppilot.errors import NotFound, SetupPilotError
from setuppilot.guardrails.commands import INSTALL_SCRIPT_WARNING, MAX_COMMAND_CHARS
from setuppilot.llm.groq import is_tracing_on
from setuppilot.tools.clone import SIGN_IN_OFF, GitHubAccess, RepoAccess
from setuppilot.tools.github_url import parse_github_url
from setuppilot.web.folder_picker import Cancelled, FolderPicker, NoFolderWindow, Picked
from setuppilot.web.github_auth import (
    add_github_routes,
    bound_user_id,
    require_binding,
    require_user_id,
    sent_key,
)
from setuppilot.web.labels import (
    GITHUB_HELP_URL,
    PAGE_LABELS,
    STATUS_LABELS,
    duration_text,
    error_label,
    repo_name,
)
from setuppilot.web.preflight import is_writable_folder
from setuppilot.web.runs import RunManager
from setuppilot.web.security import protect

_EVENT_SECONDS = 1.0
_MAX_FOLDER_CHARS = 500
_PING_SECONDS = 15.0


@dataclass(frozen=True)
class WebServices:
    settings: Settings
    stores: Stores
    runs: RunManager
    embedder: Embedder
    # None when there is no Groq key; the chat page then says so.
    make_chatbot: Callable[[], RepoChatbot] | None
    llm_problem: str | None
    preflight: Callable[[str], list[dict[str, Any]]]
    github: GitHubAccess
    # The Browse button's folder window, or why this computer has none.
    folder_picker: FolderPicker | NoFolderWindow
    pick_folder: Callable[[FolderPicker, Path], Picked | Cancelled]


class BadInput(SetupPilotError):
    """A form field is missing, too long, or the wrong type."""


def create_app(services: WebServices) -> Flask:
    app = Flask(__name__)
    token = protect(app, services.settings.web)
    chats: dict[tuple[str, str, int | None], ChatSession] = {}
    stores, settings, github = services.stores, services.settings, services.github
    add_github_routes(app, github, settings.github.max_repo_pages)
    app.jinja_env.globals |= {"page_labels": PAGE_LABELS, "github_help_url": GITHUB_HELP_URL}
    # One folder window at a time: a second one would open behind the first.
    window_open = threading.Lock()
    app.jinja_env.filters |= {
        "status_label": lambda status: STATUS_LABELS[status],
        "error_label": error_label,
        "duration": duration_text,
        "repo_name": repo_name,
        "number": lambda value: f"{value:,.0f}",
    }

    def page(name: str, **values: Any) -> str:
        return render_template(
            name, csrf_token=token, database_warning=stores.database_warning, **values
        )

    def is_page_request() -> bool:
        return not request.path.startswith("/api/")

    def error_page(code: int, title: str, message: str, details: str):
        return page("error.html", title=title, message=message, details=details), code

    def check_run_access(run_id: str) -> int | None:
        """The GitHub user a signed-in run belongs to, or None for a public run. Such a run's
        repo, folder and steps go only to that user; to anyone else there is no such run."""
        owner = stores.runs.get_run(run_id).owner_github_id
        if owner is not None and require_user_id(github) != owner:
            raise no_such_run(run_id)
        return owner

    @app.errorhandler(SetupPilotError)
    def friendly_error(error: SetupPilotError):
        if is_page_request():
            return error_page(400, "Something went wrong", error.message, error.details)
        return jsonify(error={"message": error.message, "details": error.details}), 400

    @app.errorhandler(NotFound)
    def not_found(error: NotFound):
        if is_page_request():
            return error_page(404, "Run not found", str(error), "")
        return jsonify(error={"message": str(error), "details": ""}), 404

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        if error.code == 404 and is_page_request():
            return error_page(404, "Page not found", "There is no page at this address.", "")
        # The same JSON shape as every other error, so the page banner can show it.
        body = {"message": error.description, "details": f"HTTP {error.code}"}
        return jsonify(error=body), error.code

    @app.get("/")
    def setup_page():
        picker = services.folder_picker
        return page(
            "setup.html",
            warning=INSTALL_SCRIPT_WARNING,
            folder_problem=picker.problem if isinstance(picker, NoFolderWindow) else None,
            folder_example=str(Path.home() / "work"),
        )

    @app.post("/api/pick-folder")
    def pick_folder():
        body = _json_body()
        picker = services.folder_picker
        if isinstance(picker, NoFolderWindow):
            raise BadInput(
                "This computer cannot open a folder window.",
                details=f"{picker.problem}. Type the folder's full path instead.",
            )
        start = _start_folder(body)
        if not window_open.acquire(blocking=False):
            message = "A folder window is already open."
            details = "Pick a folder in it, or close it. It may be behind this browser window."
            return jsonify(error={"message": message, "details": details}), 409
        try:
            result = services.pick_folder(picker, start)
        finally:
            window_open.release()
        if isinstance(result, Cancelled):
            return jsonify(status="cancelled", reason=result.reason)
        folder = result.path.expanduser().resolve()
        if not is_writable_folder(folder):
            raise BadInput(
                "SetupPilot cannot use that folder.",
                details=f"It could not write a test file in {folder}. Pick a folder of your "
                f"own, like {Path.home() / 'work'}.",
            )
        return jsonify(status="picked", path=str(folder), picker=picker.name)

    @app.get("/api/preflight")
    def preflight():
        return jsonify(checks=services.preflight(request.args.get("folder", "")))

    @app.post("/api/runs")
    def start_run():
        body = _json_body()
        owner = require_user_id(github) if _access(body) == "github" else None
        url = parse_github_url(_text(body, "repo_url", 300, "the GitHub URL")).url
        typed = _text(body, "dest_dir", _MAX_FOLDER_CHARS, "the folder")
        folder = Path(typed).expanduser().resolve()
        if not folder.is_dir():
            raise BadInput("That folder does not exist.", details=f"Create {folder} first.")
        # os.access says yes for every folder on Windows, so try to write a file instead.
        if not is_writable_folder(folder):
            raise BadInput(
                "SetupPilot cannot write in that folder.",
                details=f"Pick a folder in your own user folder, not {folder}.",
            )
        use_docker = body.get("use_docker") is True
        run_id = services.runs.start(url, str(folder), use_docker, owner, _subdir(body))
        return jsonify(run_id=run_id)

    @app.get("/runs/<run_id>")
    def run_page(run_id: str):
        run = stores.runs.get_run(run_id)
        # A page load cannot send the binding key, so a signed-in run's page leaves out its repo
        # and folder; the page script reads them with the key.
        if run.access == "github":
            run = replace(run, repo_url="", dest_dir="")
        return page("run.html", run=run, warning=INSTALL_SCRIPT_WARNING)

    @app.get("/api/runs/<run_id>")
    def run_view(run_id: str):
        check_run_access(run_id)
        return jsonify(services.runs.view(run_id))

    @app.get("/api/runs/<run_id>/events")
    def run_events(run_id: str):
        owner, key = check_run_access(run_id), sent_key()

        def stream() -> Iterator[str]:
            # Each event is the whole view, sent when it changes; the page needs no other state.
            last, quiet_since = None, time.monotonic()
            while True:
                # Signing out ends what this browser may see of a signed-in run.
                if owner is not None and bound_user_id(github, key) != owner:
                    return
                view = services.runs.view(run_id)
                text = json.dumps(view, default=str)
                if text != last:
                    yield f"data: {text}\n\n"
                    last, quiet_since = text, time.monotonic()
                elif time.monotonic() - quiet_since > _PING_SECONDS:
                    # Writing is the only way to notice that the tab was closed.
                    yield ": ping\n\n"
                    quiet_since = time.monotonic()
                is_cancelled = view["run"]["status"] == "cancelled"
                if view["report"] is not None or (is_cancelled and not view["is_busy"]):
                    return
                time.sleep(_EVENT_SECONDS)

        return Response(stream(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/runs/<run_id>/answer")
    def answer(run_id: str):
        check_run_access(run_id)
        body = _json_body()
        services.runs.answer(
            run_id, _text(body, "question_id", 100, "the question id"), _reply(body)
        )
        return jsonify(ok=True)

    @app.post("/api/runs/<run_id>/cancel")
    def cancel(run_id: str):
        check_run_access(run_id)
        services.runs.cancel(run_id)
        return jsonify(ok=True)

    @app.post("/api/runs/<run_id>/continue")
    def carry_on(run_id: str):
        check_run_access(run_id)
        services.runs.carry_on(run_id)
        return jsonify(ok=True)

    @app.get("/runs")
    def history_page():
        # Signed-in runs come from /api/github/runs, which needs the binding key.
        rows = runs_owned_by(None)
        statuses = {row.run.id: services.runs.shown_status(row.run) for row in rows}
        return page("history.html", rows=rows, statuses=statuses, history_limit=HISTORY_LIMIT)

    @app.get("/api/github/runs")
    def github_runs():
        viewer = require_user_id(github)
        return jsonify(
            runs=[
                {
                    "id": row.run.id,
                    "repo_url": row.run.repo_url,
                    "status": services.runs.shown_status(row.run),
                    "created_at": row.run.created_at.isoformat(),
                    "seconds": row.seconds,
                    "took": None if row.seconds is None else duration_text(row.seconds),
                    "tokens": row.tokens,
                    "error_type": row.run.error_type,
                    "message": row.run.message,
                }
                for row in runs_owned_by(viewer)
            ]
        )

    def runs_owned_by(owner_github_id: int | None) -> list[RunRow]:
        rows = recent_runs(stores.runs, stores.usage, HISTORY_LIMIT)
        return [row for row in rows if row.run.owner_github_id == owner_github_id]

    @app.get("/metrics")
    def metrics_page():
        summary = metrics_summary(stores.runs, stores.usage, stores.metrics, HISTORY_LIMIT)
        return page("metrics.html", summary=summary, history_limit=HISTORY_LIMIT)

    @app.get("/settings")
    def settings_page():
        today = datetime.now(UTC).date()
        calls = stores.usage.calls_on_day_by_model(today)
        models = [
            {"role": role, "name": m.name, "limits": m.limits, "calls": calls.get(m.name, 0)}
            for role, m in (("Main", settings.groq.big), ("Backup", settings.groq.small))
        ]
        return page(
            "settings.html",
            tokens_today=stores.usage.tokens_on_day(today),
            budget=settings.budget,
            models=models,
            llm_problem=services.llm_problem,
            is_tracing_on=is_tracing_on(),
            github_sign_in=_sign_in_facts(github),
        )

    @app.get("/chat")
    def chat_page():
        # Private repos come from /api/chat/repos, which needs the binding key.
        repos = stores.chat_index.repos(viewer_github_id=None)
        return page("chat.html", repos=repos, llm_problem=services.llm_problem)

    @app.get("/api/chat/repos")
    def chat_repos():
        viewer = bound_user_id(github, sent_key())
        visible = stores.chat_index.repos(viewer_github_id=viewer)
        public = set(stores.chat_index.repos(viewer_github_id=None))
        return jsonify(repos=visible, private=[repo for repo in visible if repo not in public])

    @app.post("/api/chat/index")
    def index_repo_for_chat():
        body = _json_body()
        access = _access(body)
        if access == "github":
            require_binding(github)
        url = _text(body, "repo_url", 300, "the GitHub URL")
        key, result = index_for_chat(
            settings, url, github, access, services.embedder, stores.chat_index
        )
        return jsonify(repo=key, chunks=result.chunks, dropped=result.dropped)

    @app.post("/api/chat/ask")
    def ask():
        if services.make_chatbot is None:
            raise BadInput("The chatbot needs a Groq key.", details=services.llm_problem or "")
        body = _json_body()
        repo = _text(body, "repo", 200, "the repo")
        question = _text(body, "question", MAX_QUESTION_CHARS, "a question")
        session.setdefault("chat_id", secrets.token_urlsafe(16))
        viewer = bound_user_id(github, sent_key())
        chat = chats.setdefault(
            (session["chat_id"], repo, viewer), ChatSession(repo, viewer_github_id=viewer)
        )
        answer = services.make_chatbot().ask(chat, question)
        return jsonify(asdict(answer))

    return app


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, "Send a JSON object.")
    return body


def _text(body: dict[str, Any], name: str, max_chars: int, label: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise BadInput(f"Please fill in {label}.", details=f"Use 1 to {max_chars} characters.")
    return value.strip()


def _access(body: dict[str, Any]) -> RepoAccess:
    """Public unless the page asks for the GitHub sign-in: the token is used only when asked."""
    access = body.get("access", "public")
    if access not in get_args(RepoAccess):
        raise BadInput(
            "Choose a public repo, or one from your GitHub sign-in.",
            details=f"access must be 'public' or 'github', not {access!r}.",
        )
    return access


def _start_folder(body: dict[str, Any]) -> Path:
    """Where the folder window opens: the folder already typed in, else the home folder."""
    typed = body.get("start", "")
    if not isinstance(typed, str) or len(typed) > _MAX_FOLDER_CHARS:
        raise BadInput(
            "The folder must be text.", details=f"Use at most {_MAX_FOLDER_CHARS} characters."
        )
    folder = Path(typed.strip() or "~").expanduser()
    return (folder if folder.is_dir() else Path.home()).resolve()


def _sign_in_facts(github: GitHubAccess) -> dict[str, str | None]:
    """For the Settings page. Never the login name: a page load cannot send the binding key."""
    signed_in = github.signed_in
    if signed_in is None:
        return {"turn_on": " ".join(SIGN_IN_OFF), "install_url": None, "notice": None}
    install_url, notice = signed_in.app.install_url, signed_in.vault.notice
    return {"turn_on": None, "install_url": install_url, "notice": notice}


def _subdir(body: dict[str, Any]) -> str | None:
    subdir = body.get("subdir")
    if subdir is not None and not isinstance(subdir, str):
        raise BadInput("The sub-folder must be text.", details="Send a folder name, or null.")
    return (subdir or "").strip() or None


def _reply(body: dict[str, Any]) -> dict[str, Any]:
    """Only answers the graph understands get through, with checked types. Whether one fits
    the question the run is waiting on is checked by SetupAgent.answer."""
    if "choice" in body:
        if not isinstance(body["choice"], int) or isinstance(body["choice"], bool):
            raise BadInput("Pick one of the options.", details="choice must be a number.")
        return {"choice": body["choice"]}
    action = body.get("action")
    if action == "edit":
        return {
            "action": "edit",
            "command": _text(body, "command", MAX_COMMAND_CHARS, "the command"),
        }
    if action in _ACTIONS:
        return {"action": action}
    raise BadInput("That answer is not one of the choices.", details=f"Got {action!r}.")


_ACTIONS = frozenset(a for answers in ANSWERS_BY_KIND.values() for a in answers)
