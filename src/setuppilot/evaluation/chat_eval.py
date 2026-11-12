"""Score the chatbot: right answer, grounded in the right file, "I don't know" when it should."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, get_args

import yaml

from setuppilot.chat.bot import ChatAnswer, Retrieval
from setuppilot.errors import SetupPilotError

# base: docs and manifests; code: only the source code says it; followup: asked right after
# `follows` in the same chat; unanswerable: about the repo, but its files do not say; offtopic:
# nothing to do with the repo.
CaseKind = Literal["base", "code", "followup", "unanswerable", "offtopic"]
_ANSWERABLE: frozenset[CaseKind] = frozenset({"base", "code", "followup"})
_KINDS: dict[str | None, CaseKind] = {kind: kind for kind in get_args(CaseKind)}


class ResultsOutdated(SetupPilotError):
    """A saved progress file was written by an older version of the evaluation."""


@dataclass(frozen=True)
class ChatCase:
    repo_url: str
    question: str
    kind: CaseKind
    expected_facts: tuple[str, ...]
    # Any one of these files counts as the right source.
    expected_sources: tuple[str, ...]
    # The question asked just before, in the same chat (follow-ups only).
    follows: str | None
    # Code facts like hashlib.sha1 or DECIMAL_PAIRS.0 sit inside longer words, so whole-word
    # matching would miss them.
    is_substring_match: bool

    @property
    def is_answerable(self) -> bool:
        return self.kind in _ANSWERABLE


@dataclass(frozen=True)
class ChatScore:
    repo_url: str
    question: str
    kind: CaseKind
    answer: str
    sources: list[str]
    # None when this mode cannot score it: search alone cannot say "I don't know".
    is_correct: bool | None
    is_grounded: bool | None
    tokens: int


def load_chat_cases(path: Path) -> list[ChatCase]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = []
    for repo in data["repos"]:
        for q in repo["questions"]:
            cases.append(_case(repo["url"], q))
    return cases


def _case(url: str, q: dict) -> ChatCase:
    question = q["question"]
    kind = _KINDS.get(q.get("kind"))
    if kind is None:
        raise ValueError(f"{question!r} has kind {q.get('kind')!r}; use one of {list(_KINDS)}.")
    facts = tuple(f.lower() for f in q.get("expected_facts", []))
    sources = tuple(q.get("expected_sources", []))
    if kind in _ANSWERABLE and not (facts and sources):
        raise ValueError(f"{question!r} needs expected_facts and expected_sources.")
    if kind not in _ANSWERABLE and (facts or sources):
        raise ValueError(f"{question!r} is {kind}, so it must not list facts or sources.")
    if (kind == "followup") != ("follows" in q):
        raise ValueError(f"{question!r}: a followup needs follows, and only a followup has it.")
    match = q.get("match", "word")
    if match not in ("word", "substring"):
        raise ValueError(f"{question!r} has match {match!r}; use word or substring.")
    return ChatCase(url, question, kind, facts, sources, q.get("follows"), match == "substring")


def score_answer(case: ChatCase, answer: ChatAnswer) -> ChatScore:
    if case.is_answerable:
        is_correct = _has_facts(case, answer.text)
        is_grounded = any(c.path in case.expected_sources for c in answer.citations)
    else:
        # For a question the repo cannot answer, "I don't know" is both correct and grounded.
        is_correct = is_grounded = answer.status in ("not_found", "out_of_scope")
    paths = [c.path for c in answer.citations]
    return ChatScore(
        case.repo_url, case.question, case.kind, answer.text, paths, is_correct, is_grounded,
        answer.tokens,
    )  # fmt: skip


def missed(case: ChatCase, why: str) -> ChatScore:
    """A question that got no usable answer (cut off, unreadable): wrong for every kind."""
    return ChatScore(case.repo_url, case.question, case.kind, why, [], False, False, 0)


def score_retrieval(case: ChatCase, found: Retrieval) -> ChatScore:
    """Without an LLM: did search find a right file, and is every fact in the text the LLM
    would get? A question the gate stops never reaches the LLM, so it is a miss."""
    paths = [c.path for c in found.hits]
    if not case.is_answerable:
        return ChatScore(case.repo_url, case.question, case.kind, "(retrieval only)", paths,
                         None, None, 0)  # fmt: skip
    # Each source goes to the LLM under a label with its path, so the path counts too.
    context = "\n".join(c.search_text for c in found.sources)
    is_correct = not found.is_gated and _has_facts(case, context)
    is_grounded = not found.is_gated and any(p in case.expected_sources for p in paths)
    return ChatScore(
        case.repo_url, case.question, case.kind, "(retrieval only)", paths, is_correct,
        is_grounded, 0,
    )  # fmt: skip


def _has_facts(case: ChatCase, text: str) -> bool:
    lower = text.lower()
    if case.is_substring_match:
        return all(fact in lower for fact in case.expected_facts)
    return all(_has_phrase(lower, fact) for fact in case.expected_facts)


def _has_phrase(text: str, fact: str) -> bool:
    """Whole words, plus a plain ending ("signed" for "sign"), so "mit" never matches
    "permitted"."""
    pattern = rf"(?<![\w.]){re.escape(fact)}(?:s|es|ed|ing)?(?!\w|\.\w)"
    return re.search(pattern, text) is not None


def results_table(scores: list[ChatScore]) -> str:
    """Markdown: one row per repo and a total row, then one row per kind of question."""
    head = '| Correct | Grounded | Correct "I don\'t know" |'
    lines = [f"| Repo | Questions {head} Avg tokens |", "|---|---|---|---|---|---|"]
    groups = {s.repo_url: [x for x in scores if x.repo_url == s.repo_url] for s in scores}
    for name, group in [*groups.items(), ("**Total**", scores)]:
        lines.append(f"| {name} | {len(group)} | {_shares(group)} | {_tokens(group)} |")
    lines += ["", f"| Kind | Questions {head}", "|---|---|---|---|---|"]
    for kind in get_args(CaseKind):
        if group := [s for s in scores if s.kind == kind]:
            lines.append(f"| {kind} | {len(group)} | {_shares(group)} |")
    return "\n".join(lines)


def _shares(group: list[ChatScore]) -> str:
    answerable = [s for s in group if s.kind in _ANSWERABLE]
    unanswerable = [s for s in group if s.kind not in _ANSWERABLE]
    return (
        f"{_share(answerable, 'is_correct')} | {_share(answerable, 'is_grounded')} | "
        f"{_share(unanswerable, 'is_correct')}"
    )


def _tokens(group: list[ChatScore]) -> str:
    return f"{sum(s.tokens for s in group) / max(len(group), 1):.0f}"


def _share(scores: list[ChatScore], field: str) -> str:
    scored = [getattr(s, field) for s in scores if getattr(s, field) is not None]
    if not scored:
        return "-"
    return f"{sum(scored)}/{len(scored)} ({sum(scored) / len(scored):.0%})"


def load_done(path: Path) -> list[ChatScore]:
    """Scores saved by an earlier, stopped run, so the evaluation can carry on later."""
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        return [ChatScore(**json.loads(line)) for line in lines if line]
    except TypeError as error:
        raise ResultsOutdated(
            f"{path} was written by an older version of the chat evaluation.",
            details=f"Its scores do not fit the new format ({error}). Delete it, or pass another "
            "--results folder, then run the evaluation again.",
        ) from error


def save_score(path: Path, score: ChatScore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(score)) + "\n")
