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

from setuppilot.chat.bot import MAX_QUESTION_CHARS, ChatSession, RepoChatbot
from setuppilot.chat.embed import Embedder
from setuppilot.chat.repos import index_github_repo
from setuppilot.config import Settings
from setuppilot.db.reports import HISTORY_LIMIT, metrics_summary, recent_runs
from setuppilot.db.stores import Stores
from setuppilot.errors import NotFound, SetupPilotError
from setuppilot.guardrails.commands import INSTALL_SCRIPT_WARNING
from setuppilot.llm.groq import is_tracing_on
from setuppilot.tools.github_url import parse_github_url
from setuppilot.web.runs import RunManager
from setuppilot.web.security import protect

_EVENT_SECONDS = 1.0
_PING_SECONDS = 15.0
_MAX_COMMAND_CHARS = 1000


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

    def page(name: str, **values: Any) -> str:
        return render_template(
            name, csrf_token=token, database_warning=stores.database_warning, **values
        )

    @app.errorhandler(SetupPilotError)
    def friendly_error(error: SetupPilotError):
        return jsonify(error={"message": error.message, "details": error.details}), 400

    @app.errorhandler(NotFound)
    def not_found(error: NotFound):
        return jsonify(error={"message": str(error), "details": ""}), 404

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
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
        url = parse_github_url(_text(body, "repo_url", 300)).url
        folder = Path(_text(body, "dest_dir", 500)).expanduser().resolve()
        if not folder.is_dir():
            raise BadInput("That folder does not exist.", details=f"Create {folder} first.")
        run_id = services.runs.start(url, str(folder), body.get("use_docker") is True)
        return jsonify(run_id=run_id)

    @app.get("/runs/<run_id>")
    def run_page(run_id: str):
        stores.runs.get_run(run_id)
        return page("run.html", run_id=run_id, warning=INSTALL_SCRIPT_WARNING)

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
                if view["report"] is not None or view["run"]["status"] == "cancelled":
                    return
                time.sleep(_EVENT_SECONDS)

        return Response(stream(), mimetype="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/runs/<run_id>/answer")
    def answer(run_id: str):
        body = _json_body()
        services.runs.answer(run_id, _text(body, "question_id", 100), _reply(body))
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
        return page("history.html", rows=rows)

    @app.get("/metrics")
    def metrics_page():
        summary = metrics_summary(stores.runs, stores.usage, stores.metrics, HISTORY_LIMIT)
        return page("metrics.html", summary=summary)

    @app.get("/settings")
    def settings_page():
        today = datetime.now(UTC).date()
        calls = stores.usage.calls_on_day_by_model(today)
        models = [
            {"role": role, "name": m.name, "limits": m.limits, "calls": calls.get(m.name, 0)}
            for role, m in (("BIG", settings.groq.big), ("SMALL", settings.groq.small))
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
        return page("chat.html", repos=stores.chat_index.repos(), llm_problem=services.llm_problem)

    @app.post("/api/chat/index")
    def index_for_chat():
        url = _text(_json_body(), "repo_url", 300)
        key, result = index_github_repo(settings, url, services.embedder, stores.chat_index)
        return jsonify(repo=key, chunks=result.chunks, dropped=result.dropped)

    @app.post("/api/chat/ask")
    def ask():
        if services.make_chatbot is None:
            raise BadInput("The chatbot needs a Groq key.", details=services.llm_problem or "")
        body = _json_body()
        repo = _text(body, "repo", 200)
        question = _text(body, "question", MAX_QUESTION_CHARS)
        session.setdefault("chat_id", secrets.token_urlsafe(16))
        chat = chats.setdefault((session["chat_id"], repo), ChatSession(repo))
        answer = services.make_chatbot().ask(chat, question)
        return jsonify(asdict(answer))

    return app


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, "Send a JSON object.")
    return body


def _text(body: dict[str, Any], name: str, max_chars: int) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise BadInput(
            f"Please fill in {name.replace('_', ' ')}.",
            details=f"{name} must be text of 1 to {max_chars} characters.",
        )
    return value.strip()


def _reply(body: dict[str, Any]) -> dict[str, Any]:
    """Only the three answers the graph understands get through, with checked types."""
    if "choice" in body:
        if not isinstance(body["choice"], int) or isinstance(body["choice"], bool):
            raise BadInput("Pick one of the options.", details="choice must be a number.")
        return {"choice": body["choice"]}
    action = body.get("action")
    if action in ("approve", "reject"):
        return {"action": action}
    if action == "edit":
        return {"action": "edit", "command": _text(body, "command", _MAX_COMMAND_CHARS)}
    raise BadInput("Choose approve, reject or edit.", details=f"Got {action!r}.")
