"""The Flask web app: setup runs with live progress and approvals, the chatbot, history, metrics."""

import json
import secrets
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from setuppilot.agent.driver import ANSWERS_BY_KIND
from setuppilot.chat.bot import MAX_QUESTION_CHARS, ChatSession, RepoChatbot
from setuppilot.chat.embed import Embedder
from setuppilot.chat.repos import index_github_repo
from setuppilot.config import Settings
from setuppilot.db.reports import HISTORY_LIMIT, metrics_summary, recent_runs
from setuppilot.db.stores import Stores
from setuppilot.errors import NotFound, SetupPilotError
from setuppilot.guardrails.commands import INSTALL_SCRIPT_WARNING, MAX_COMMAND_CHARS
from setuppilot.llm.groq import is_tracing_on
from setuppilot.tools.github_url import parse_github_url
from setuppilot.web.labels import PAGE_LABELS, STATUS_LABELS, duration_text, error_label, repo_name
from setuppilot.web.runs import RunManager
from setuppilot.web.security import protect

_EVENT_SECONDS = 1.0
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


class BadInput(SetupPilotError):
    """A form field is missing, too long, or the wrong type."""


def create_app(services: WebServices) -> Flask:
    app = Flask(__name__)
    token = protect(app, services.settings.web)
    chats: dict[tuple[str, str], ChatSession] = {}
    stores, settings = services.stores, services.settings
    app.jinja_env.globals["page_labels"] = PAGE_LABELS
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
        return page("setup.html", warning=INSTALL_SCRIPT_WARNING)

    @app.get("/api/preflight")
    def preflight():
        return jsonify(checks=services.preflight(request.args.get("folder", "")))

    @app.post("/api/runs")
    def start_run():
        body = _json_body()
        url = parse_github_url(_text(body, "repo_url", 300, "the GitHub URL")).url
        folder = Path(_text(body, "dest_dir", 500, "the folder")).expanduser().resolve()
        if not folder.is_dir():
            raise BadInput("That folder does not exist.", details=f"Create {folder} first.")
        run_id = services.runs.start(url, str(folder), body.get("use_docker") is True)
        return jsonify(run_id=run_id)

    @app.get("/runs/<run_id>")
    def run_page(run_id: str):
        run = stores.runs.get_run(run_id)
        return page("run.html", run=run, warning=INSTALL_SCRIPT_WARNING)

    @app.get("/api/runs/<run_id>")
    def run_view(run_id: str):
        return jsonify(services.runs.view(run_id))

    @app.get("/api/runs/<run_id>/events")
    def run_events(run_id: str):
        stores.runs.get_run(run_id)

        def stream() -> Iterator[str]:
            # Each event is the whole view, sent when it changes; the page needs no other state.
            last, quiet_since = None, time.monotonic()
            while True:
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
        body = _json_body()
        services.runs.answer(
            run_id, _text(body, "question_id", 100, "the question id"), _reply(body)
        )
        return jsonify(ok=True)

    @app.post("/api/runs/<run_id>/cancel")
    def cancel(run_id: str):
        stores.runs.get_run(run_id)
        services.runs.cancel(run_id)
        return jsonify(ok=True)

    @app.post("/api/runs/<run_id>/continue")
    def carry_on(run_id: str):
        services.runs.carry_on(run_id)
        return jsonify(ok=True)

    @app.get("/runs")
    def history_page():
        rows = recent_runs(stores.runs, stores.usage, HISTORY_LIMIT)
        statuses = {row.run.id: services.runs.shown_status(row.run) for row in rows}
        return page("history.html", rows=rows, statuses=statuses, history_limit=HISTORY_LIMIT)

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
        )

    @app.get("/chat")
    def chat_page():
        repos = stores.chat_index.repos(viewer_github_id=None)
        return page("chat.html", repos=repos, llm_problem=services.llm_problem)

    @app.post("/api/chat/index")
    def index_for_chat():
        url = _text(_json_body(), "repo_url", 300, "the GitHub URL")
        key, result = index_github_repo(settings, url, services.embedder, stores.chat_index)
        return jsonify(repo=key, chunks=result.chunks, dropped=result.dropped)

    @app.post("/api/chat/ask")
    def ask():
        if services.make_chatbot is None:
            raise BadInput("The chatbot needs a Groq key.", details=services.llm_problem or "")
        body = _json_body()
        repo = _text(body, "repo", 200, "the repo")
        question = _text(body, "question", MAX_QUESTION_CHARS, "a question")
        session.setdefault("chat_id", secrets.token_urlsafe(16))
        chat = chats.setdefault(
            (session["chat_id"], repo), ChatSession(repo, viewer_github_id=None)
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
