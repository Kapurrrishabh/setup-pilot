"""Terminal front end for the repo chatbot: index a repo, then ask about it."""

import sys
from collections.abc import Callable

from setuppilot.chat.bot import AnswerCutOff, AnswerUnreadable, ChatSession, RepoChatbot, place
from setuppilot.chat.embed import LocalEmbedder
from setuppilot.chat.repos import index_github_repo
from setuppilot.config import Settings
from setuppilot.db.stores import Stores
from setuppilot.llm.groq import build_llm_client


def index_command(settings: Settings, stores: Stores, url: str) -> None:
    embedder = LocalEmbedder(settings.chat.embedding_model, settings.chat.embedding_dimensions)
    key, result = index_github_repo(settings, url, embedder, stores.chat_index)
    cut = (
        f" ({result.dropped} files were left out, whole or in part, to keep the database small)"
        if result.dropped
        else ""
    )
    hint = (
        "Without a database this index is gone when this command ends, so index and ask about "
        "it in the web app's Chat page instead (setuppilot web)."
        if stores.database_warning
        else f"Ask about it with: setuppilot chat {key}"
    )
    print(f"Indexed {key}: {result.chunks} chunks{cut}. {hint}")


def chat_command(
    settings: Settings, stores: Stores, repo_key: str, ask: Callable[[str], str]
) -> None:
    # The terminal has no GitHub sign-in, so it sees public repos only.
    if repo_key not in stores.chat_index.repos(viewer_github_id=None):
        how = (
            "Without a database, an index made by setuppilot index is gone when that command "
            "ends. Set SUPABASE_DB_URL in .env, or use the web app's Chat page (setuppilot web)."
            if stores.database_warning
            else "Run: setuppilot index <its GitHub URL>"
        )
        print(f"{repo_key} is not indexed yet. {how}", file=sys.stderr)
        raise SystemExit(1)
    llm = build_llm_client(settings, stores.usage, stores.cache, stores.metrics)
    embedder = LocalEmbedder(settings.chat.embedding_model, settings.chat.embedding_dimensions)
    bot = RepoChatbot(settings.chat, stores.chat_index, embedder, llm)
    session = ChatSession(repo_key, viewer_github_id=None)
    print(f"Ask about {repo_key}. Type q to stop.")
    while (question := ask("> ").strip()) not in {"q", ""}:
        try:
            answer = bot.ask(session, question)
        except (AnswerCutOff, AnswerUnreadable) as error:
            # Only this answer failed: the chat and its history go on.
            print(f"{error.message}\nDetails: {error.details}", file=sys.stderr)
            continue
        print(answer.text)
        if answer.citations:
            places = (
                f"[{c.n}] {place(c.path, c.start_line, c.end_line)}" for c in answer.citations
            )
            print("Sources: " + ", ".join(places))
        if answer.looked_at:
            print("Closest files: " + ", ".join(answer.looked_at))
        print(f"(tokens: {answer.tokens})")
