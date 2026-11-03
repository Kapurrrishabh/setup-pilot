"""Run the chatbot evaluation on real repos. Stops cleanly at a limit and carries on next time."""

import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from setuppilot.chat.bot import AnswerCutOff, ChatAnswer, ChatSession, RepoChatbot, search_chunks
from setuppilot.chat.embed import Embedder
from setuppilot.chat.repos import index_github_repo, repo_key
from setuppilot.config import GroqModel, Settings
from setuppilot.db.stores import Stores
from setuppilot.errors import BudgetExceeded, LLMUnavailable, SetupPilotError
from setuppilot.evaluation.chat_eval import (
    ChatCase,
    ChatScore,
    load_done,
    results_table,
    save_score,
    score_answer,
    score_retrieval,
)
from setuppilot.llm.client import LLMClient
from setuppilot.llm.transport import Messages, Reply

Mode = Literal["full", "retrieval", "replay"]


class CacheMiss(SetupPilotError):
    """Replay mode has no saved answer. Not LLMUnavailable, so no switch to SMALL is counted."""


class CacheOnlyTransport:
    """Replay mode: every answer must already be in the cache, so a rerun costs nothing."""

    def send(self, model: GroqModel, messages: Messages, max_tokens: int, is_json: bool) -> Reply:
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
    for url in dict.fromkeys(c.repo_url for c in cases):
        _make_sure_indexed(settings, stores, url, embedder)
    bot = RepoChatbot(settings.chat, stores.chat_index, embedder, llm) if llm else None
    for case in cases:
        if (case.repo_url, case.question) in done:
            continue
        key = repo_key(case.repo_url)
        if bot is None:
            (vector,) = embedder.embed([case.question])
            hits = search_chunks(stores.chat_index, settings.chat, key, vector)
            score = score_retrieval(case, hits, settings.chat.min_similarity)
        else:
            try:
                score = score_answer(case, bot.ask(ChatSession(key), case.question))
            except AnswerCutOff:
                # Counted as a miss, not a stop: a rerun would ask the same question and cut again.
                score = score_answer(case, ChatAnswer("(answer cut off)", [], True, False, 0))
            except (LLMUnavailable, BudgetExceeded, CacheMiss) as error:
                print(f"Stopped: {error.message} Run the same command later to carry on.")
                break
        save_score(progress, score)
    scores = load_done(progress)
    table = results_table(scores)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    heading = f"# Chatbot evaluation ({mode}), {stamp} UTC"
    done_line = f"{len(scores)} of {len(cases)} questions done."
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / f"chat-{mode}-{stamp}.md").write_text(f"{heading}\n\n{done_line}\n\n{table}\n")
    print(table)
    return scores


def _make_sure_indexed(settings: Settings, stores: Stores, url: str, embedder: Embedder) -> None:
    if repo_key(url) not in stores.chat_index.repos():
        index_github_repo(settings, url, embedder, stores.chat_index)
