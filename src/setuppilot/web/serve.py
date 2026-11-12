"""Build everything the web app needs, and start it on this machine only."""

from functools import partial

from setuppilot.agent.wiring import build_agent
from setuppilot.chat.bot import RepoChatbot
from setuppilot.chat.embed import LocalEmbedder
from setuppilot.config import Settings
from setuppilot.db.stores import open_stores
from setuppilot.errors import LLMUnavailable
from setuppilot.llm.groq import build_llm_client
from setuppilot.tools.prereqs import SystemToolProbe
from setuppilot.web.app import WebServices, create_app
from setuppilot.web.preflight import run_preflight, windows_facts
from setuppilot.web.runs import CommandTracker, RunManager


def build_services(settings: Settings) -> WebServices:
    stores = open_stores(settings)
    try:
        llm = build_llm_client(settings, stores.usage, stores.cache, stores.metrics)
        llm_problem = None
    except LLMUnavailable as error:
        llm, llm_problem = None, f"{error.message} {error.details}"
    tracker = CommandTracker()
    agent = build_agent(settings, stores, llm, on_start=tracker.track)
    runs = RunManager(agent, stores.runs, tracker)
    embedder = LocalEmbedder(settings.chat.embedding_model, settings.chat.embedding_dimensions)
    return WebServices(
        settings=settings,
        stores=stores,
        runs=runs,
        embedder=embedder,
        make_chatbot=(
            partial(RepoChatbot, settings.chat, stores.chat_index, embedder, llm) if llm else None
        ),
        llm_problem=llm_problem,
        preflight=lambda folder: run_preflight(stores, SystemToolProbe(), folder, windows_facts()),
    )


def serve(settings: Settings) -> None:
    app = create_app(build_services(settings))
    web = settings.web
    print(
        f"Starting SetupPilot at http://{web.host}:{web.port} (only this machine can open it). "
        "If the port is busy, change web.port in config.yaml."
    )
    app.run(host=web.host, port=web.port, threaded=True, debug=False, use_reloader=False)
