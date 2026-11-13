"""Run the chatbot evaluation on real repos. Stops cleanly at a limit and carries on next time."""

import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from setuppilot.chat.bot import (
    AnswerCutOff,
    AnswerUnreadable,
    ChatSession,
    RepoChatbot,
    retrieve,
)
from setuppilot.chat.embed import Embedder
from setuppilot.chat.repos import index_for_chat, repo_key
from setuppilot.config import GroqModel, Settings
from setuppilot.db.stores import Stores
from setuppilot.errors import BudgetExceeded, LLMUnavailable, SetupPilotError
from setuppilot.evaluation.chat_eval import (
    ChatCase,
    ChatScore,
    load_done,
    missed,
    results_table,
    save_score,
    score_answer,
    score_retrieval,
)
from setuppilot.llm.client import LLMClient
from setuppilot.llm.transport import Messages, Reply, ResponseFormat
from setuppilot.tools.clone import GitHubAccess, public_github

Mode = Literal["full", "retrieval", "replay"]
_RETRIEVAL_NOTE = (
    "Search only, no LLM: Correct means every expected fact is in the text the LLM would get "
    "(the repo card and the chunks found); Grounded means search found an expected file."
)


class CacheMiss(SetupPilotError):
    """Replay mode has no saved answer. Not LLMUnavailable, so no switch to SMALL is counted."""


class CacheOnlyTransport:
    """Replay mode: every answer must already be in the cache, so a rerun costs nothing."""

    def send(
        self, model: GroqModel, messages: Messages, max_tokens: int, response_format: ResponseFormat
    ) -> Reply:
        raise CacheMiss(
            "Replay mode: this answer is not in the cache.",
            details=f"{model.name} was not called. Run the evaluation in full mode first.",
        )


def replay_client(settings: Settings, stores: Stores) -> LLMClient:
    return LLMClient(
        settings.groq, settings.budget, CacheOnlyTransport(), stores.usage, stores.cache,
        stores.metrics, clock=time.time, sleep=time.sleep, random=random.random,
    )  # fmt: skip


def run_chat_eval(
    settings: Settings, stores: Stores, cases: list[ChatCase], mode: Mode,
    results_dir: Path, embedder: Embedder, llm: LLMClient | None,
) -> list[ChatScore]:  # fmt: skip
    progress = results_dir / f"chat-{mode}.jsonl"
    done = {(s.repo_url, s.question) for s in load_done(progress)}
    if done:
        print(
            f"Carrying on: {len(done)} questions were scored by an earlier run ({progress}). If "
            "chat settings changed since, delete that file and re-index each repo first."
        )
    github = public_github()
    for url in dict.fromkeys(c.repo_url for c in cases):
        _make_sure_indexed(settings, stores, url, github, embedder)
    bot = RepoChatbot(settings.chat, stores.chat_index, embedder, llm) if llm else None
    for case in cases:
        if (case.repo_url, case.question) in done:
            continue
        # The eval has no GitHub sign-in, so it reads public repos only.
        session = ChatSession(repo_key(case.repo_url), viewer_github_id=None)
        if bot is None:
            if case.follows:
                # As if the earlier question was just asked: search reads only that question.
                session.turns.append((case.follows, ""))
            found = retrieve(stores.chat_index, embedder, settings.chat, session, case.question)
            score = score_retrieval(case, found)
        else:
            try:
                if case.follows:
                    bot.ask(session, case.follows)
                score = score_answer(case, bot.ask(session, case.question))
            except AnswerCutOff:
                # Counted as a miss, not a stop: a rerun would ask the same question and cut again.
                score = missed(case, "(answer cut off)")
            except AnswerUnreadable:
                score = missed(case, "(answer not readable)")
            except (LLMUnavailable, BudgetExceeded, CacheMiss) as error:
                print(f"Stopped: {error.message} Run the same command later to carry on.")
                break
        save_score(progress, score)
    scores = load_done(progress)
    table = results_table(scores)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    heading = f"# Chatbot evaluation ({mode}), {stamp} UTC"
    done_line = f"{len(scores)} of {len(cases)} questions done."
    note = f"{_RETRIEVAL_NOTE}\n\n" if mode == "retrieval" else ""
    results_dir.mkdir(parents=True, exist_ok=True)
    report = f"{heading}\n\n{done_line}\n\n{note}{table}\n"
    (results_dir / f"chat-{mode}-{stamp}.md").write_text(report, encoding="utf-8")
    print(table)
    return scores


def _make_sure_indexed(
    settings: Settings, stores: Stores, url: str, github: GitHubAccess, embedder: Embedder
) -> None:
    key, index = repo_key(url), stores.chat_index
    is_current = key in index.repos(viewer_github_id=None) and (
        index.repo(key, viewer_github_id=None).embedding_model == embedder.model_name
    )
    if not is_current:
        index_for_chat(settings, url, github, "public", embedder, index)
