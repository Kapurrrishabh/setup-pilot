"""Terminal front end for the repo chatbot: index a repo, then ask about it."""

import sys
from collections.abc import Callable

from setuppilot.chat.bot import ChatSession, RepoChatbot
from setuppilot.chat.embed import LocalEmbedder
from setuppilot.chat.repos import index_github_repo
from setuppilot.config import Settings
from setuppilot.db.stores import Stores
from setuppilot.llm.groq import build_llm_client


def index_command(settings: Settings, stores: Stores, url: str) -> None:
    embedder = LocalEmbedder(settings.chat.embedding_model, settings.chat.embedding_dimensions)
    key, result = index_github_repo(settings, url, embedder, stores.chat_index)
    cut = (
        f" ({result.dropped} more were left out to keep the database small)"
        if result.dropped
        else ""
    )
    print(f"Indexed {key}: {result.chunks} chunks{cut}. Ask about it with: setuppilot chat {key}")


def chat_command(
    settings: Settings, stores: Stores, repo_key: str, ask: Callable[[str], str]
) -> None:
    if repo_key not in stores.chat_index.repos():
        print(
            f"{repo_key} is not indexed yet. Run: setuppilot index <its GitHub URL>",
            file=sys.stderr,
        )
        raise SystemExit(1)
    llm = build_llm_client(settings, stores.usage, stores.cache, stores.metrics)
    embedder = LocalEmbedder(settings.chat.embedding_model, settings.chat.embedding_dimensions)
    bot = RepoChatbot(settings.chat, stores.chat_index, embedder, llm)
    session = ChatSession(repo_key)
    print(f"Ask about {repo_key}. Type q to stop.")
    while (question := ask("> ").strip()) not in {"q", ""}:
        answer = bot.ask(session, question)
        print(answer.text)
        if answer.sources:
            print("Sources: " + ", ".join(answer.sources))
        print(f"(tokens: {answer.tokens})")
