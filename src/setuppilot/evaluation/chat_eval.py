"""Score the chatbot: right answer, grounded in the right file, "I don't know" when it should."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from setuppilot.chat.bot import NOT_FOUND, ChatAnswer
from setuppilot.chat.index import Hit


@dataclass(frozen=True)
class ChatCase:
    repo_url: str
    question: str
    is_answerable: bool
    expected_facts: tuple[str, ...]
    expected_source: str | None


@dataclass(frozen=True)
class ChatScore:
    repo_url: str
    question: str
    is_answerable: bool
    answer: str
    sources: list[str]
    is_correct: bool
    is_grounded: bool
    tokens: int


def load_chat_cases(path: Path) -> list[ChatCase]:
    data = yaml.safe_load(path.read_text())
    cases = []
    for repo in data["repos"]:
        for q in repo["questions"]:
            is_answerable = bool(q["answerable"])
            facts = tuple(f.lower() for f in q.get("expected_facts", []))
            source = q.get("expected_source")
            if is_answerable and not (facts and source):
                raise ValueError(f"{q['question']!r} needs expected_facts and expected_source.")
            cases.append(ChatCase(repo["url"], q["question"], is_answerable, facts, source))
    return cases


def score_answer(case: ChatCase, answer: ChatAnswer) -> ChatScore:
    if case.is_answerable:
        is_correct = all(_has_phrase(answer.text, fact) for fact in case.expected_facts)
        is_grounded = case.expected_source in answer.sources
    else:
        # For a question the repo cannot answer, "I don't know" is both correct and grounded.
        is_correct = is_grounded = answer.text == NOT_FOUND
    return ChatScore(
        case.repo_url, case.question, case.is_answerable, answer.text, answer.sources,
        is_correct, is_grounded, answer.tokens,
    )  # fmt: skip


def _has_phrase(text: str, fact: str) -> bool:
    """Whole words, plus a plain ending ("signed" for "sign"), so "mit" never matches
    "permitted"."""
    pattern = rf"(?<![\w.]){re.escape(fact)}(?:s|es|ed|ing)?(?!\w|\.\w)"
    return re.search(pattern, text.lower()) is not None


def score_retrieval(case: ChatCase, hits: list[Hit], min_similarity: float) -> ChatScore:
    """Without an LLM: did search find the right file, or find nothing for an unanswerable one?"""
    found = [h.chunk.path for h in hits if h.similarity >= min_similarity]
    if case.is_answerable:
        is_correct = is_grounded = case.expected_source in found
    else:
        is_correct = is_grounded = not found
    return ChatScore(
        case.repo_url, case.question, case.is_answerable, "(retrieval only)", found,
        is_correct, is_grounded, 0,
    )  # fmt: skip


def results_table(scores: list[ChatScore]) -> str:
    """Markdown table: one row per repo and a total row."""
    lines = [
        '| Repo | Questions | Correct | Grounded | Correct "I don\'t know" | Avg tokens |',
        "|---|---|---|---|---|---|",
    ]
    groups = {s.repo_url: [x for x in scores if x.repo_url == s.repo_url] for s in scores}
    for name, group in [*groups.items(), ("**Total**", scores)]:
        answerable = [s for s in group if s.is_answerable]
        unanswerable = [s for s in group if not s.is_answerable]
        lines.append(
            f"| {name} | {len(group)} | {_share(answerable, 'is_correct')} | "
            f"{_share(answerable, 'is_grounded')} | {_share(unanswerable, 'is_correct')} | "
            f"{sum(s.tokens for s in group) / max(len(group), 1):.0f} |"
        )
    return "\n".join(lines)


def _share(scores: list[ChatScore], field: str) -> str:
    if not scores:
        return "-"
    hits = sum(getattr(s, field) for s in scores)
    return f"{hits}/{len(scores)} ({hits / len(scores):.0%})"


def load_done(path: Path) -> list[ChatScore]:
    """Scores saved by an earlier, stopped run, so the evaluation can carry on later."""
    if not path.exists():
        return []
    return [ChatScore(**json.loads(line)) for line in path.read_text().splitlines() if line]


def save_score(path: Path, score: ChatScore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(asdict(score)) + "\n")
