"""Build everything the web app needs, and start it on this machine only."""

from setuppilot.agent.wiring import build_agent
from setuppilot.chat.bot import RepoChatbot
from setuppilot.chat.embed import LocalEmbedder
from setuppilot.config import Settings
from setuppilot.db.stores import open_stores
from setuppilot.errors import LLMUnavailable
from setuppilot.llm.groq import build_llm_client
from setuppilot.tools.prereqs import SystemToolProbe
from setuppilot.web.app import WebServices, create_app
from setuppilot.web.preflight import run_preflight
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

    def make_chatbot() -> RepoChatbot:
        return RepoChatbot(settings.chat, stores.chat_index, embedder, llm)

    return WebServices(
        settings=settings,
        stores=stores,
        runs=runs,
        embedder=embedder,
        make_chatbot=make_chatbot if llm else None,
        llm_problem=llm_problem,
        preflight=lambda folder: run_preflight(stores, SystemToolProbe(), folder),
    )


def serve(settings: Settings) -> None:
    app = create_app(build_services(settings))
    web = settings.web
    print(f"SetupPilot is running at http://{web.host}:{web.port} (only this machine can open it).")
    app.run(host=web.host, port=web.port, threaded=True, debug=False, use_reloader=False)
