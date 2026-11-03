import json

import pytest

from setuppilot.chat.bot import NOT_FOUND, ChatAnswer, index_repo
from setuppilot.chat.chunks import Chunk
from setuppilot.chat.index import Hit, InMemoryChunkIndex
from setuppilot.config import load_settings
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.db.runs import InMemoryRunStore
from setuppilot.db.stores import Stores
from setuppilot.errors import LLMUnavailable
from setuppilot.evaluation.chat_eval import (
    ChatCase,
    load_chat_cases,
    results_table,
    score_answer,
    score_retrieval,
)
from setuppilot.evaluation.run_chat import CacheOnlyTransport, run_chat_eval
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.usage import InMemoryUsageStore
from tests.fakes import FakeEmbedder, FakeTransport, groq_config, make_client, make_repo, reply
from tests.test_config import REAL_CONFIG

SETTINGS = load_settings(REAL_CONFIG)
URL = "https://github.com/someone/demo"
LICENSE_CASE = ChatCase(URL, "What license is it under?", True, ("mit",), "LICENSE")
UNKNOWN_CASE = ChatCase(URL, "What is the company's revenue?", False, (), None)


def test_correct_and_grounded_answer_scores_both():
    score = score_answer(
        LICENSE_CASE, ChatAnswer("It is MIT licensed.", ["LICENSE"], True, True, 9)
    )
    assert (score.is_correct, score.is_grounded) == (True, True)


def test_right_words_from_the_wrong_file_are_not_grounded():
    score = score_answer(LICENSE_CASE, ChatAnswer("MIT.", ["README.md"], True, True, 9))
    assert (score.is_correct, score.is_grounded) == (True, False)


def test_i_dont_know_is_right_only_for_unanswerable_questions():
    idk = ChatAnswer(NOT_FOUND, [], True, False, 0)
    assert score_answer(UNKNOWN_CASE, idk).is_correct
    assert not score_answer(LICENSE_CASE, idk).is_correct


def test_retrieval_only_checks_the_expected_file_is_found():
    hits = [Hit(Chunk("LICENSE", "", "MIT"), 0.5), Hit(Chunk("README.md", "", "x"), 0.1)]
    assert score_retrieval(LICENSE_CASE, hits, 0.3).is_correct
    assert not score_retrieval(UNKNOWN_CASE, hits, 0.3).is_correct


def test_question_file_without_facts_for_an_answerable_question_is_refused(tmp_path):
    path = tmp_path / "q.yaml"
    path.write_text("repos:\n  - url: u\n    questions:\n      - {question: q, answerable: true}\n")
    with pytest.raises(ValueError, match="needs expected_facts"):
        load_chat_cases(path)


def test_table_has_a_row_per_repo_and_a_total():
    scores = [
        score_answer(LICENSE_CASE, ChatAnswer("MIT", ["LICENSE"], True, True, 10)),
        score_answer(UNKNOWN_CASE, ChatAnswer(NOT_FOUND, [], True, False, 0)),
    ]
    table = results_table(scores)
    assert f"| {URL} | 2 | 1/1 (100%) | 1/1 (100%) | 1/1 (100%) | 5 |" in table
    assert "| **Total** |" in table


def stores_with(index):
    return Stores(
        InMemoryRunStore(), InMemoryUsageStore(), InMemoryCache(), InMemoryMetricsStore(),
        index, None, None,
    )  # fmt: skip


@pytest.fixture
def stores(tmp_path):
    index = InMemoryChunkIndex()
    folder = make_repo(tmp_path / "demo", {"LICENSE": "MIT License text", "README.md": "# Demo"})
    index_repo("someone/demo", URL, folder, SETTINGS.chat, 2000, FakeEmbedder(), index)
    return stores_with(index)


def test_retrieval_mode_needs_no_llm(tmp_path, stores):
    scores = run_chat_eval(
        SETTINGS, stores, [LICENSE_CASE], "retrieval", tmp_path / "out", FakeEmbedder(), None
    )
    assert scores[0].is_grounded
    assert list((tmp_path / "out").glob("chat-retrieval-*.md"))


def test_stopped_run_carries_on_where_it_left_off(tmp_path, stores):
    answer = json.dumps({"answer": "MIT", "found": True, "sources": ["LICENSE"]})
    limit = LLMUnavailable("Daily limit reached.", "x")
    # The BIG model and then the SMALL one both hit the limit on the second question.
    first = make_client(FakeTransport(reply(answer), limit, limit))
    cases = [LICENSE_CASE, ChatCase(URL, "Is it MIT License text?", True, ("mit",), "LICENSE")]
    done = run_chat_eval(SETTINGS, stores, cases, "full", tmp_path, FakeEmbedder(), first)
    assert len(done) == 1
    second = make_client(FakeTransport(reply(answer)))
    assert (
        len(run_chat_eval(SETTINGS, stores, cases, "full", tmp_path, FakeEmbedder(), second)) == 2
    )


def test_replay_mode_never_calls_groq():
    with pytest.raises(LLMUnavailable, match="not in the cache"):
        CacheOnlyTransport().send(groq_config().big, [], 10, True)


def test_facts_must_be_whole_words():
    case = ChatCase(URL, "License?", True, ("mit",), "LICENSE")
    wrong = score_answer(case, ChatAnswer("Use is permitted freely.", ["LICENSE"], True, True, 1))
    right = score_answer(case, ChatAnswer("It is MIT.", ["LICENSE"], True, True, 1))
    assert (wrong.is_correct, right.is_correct) == (False, True)


def test_version_facts_do_not_match_longer_versions():
    case = ChatCase(URL, "Version?", True, ("1.0",), "pyproject.toml")
    assert not score_answer(
        case, ChatAnswer("It is 1.0.18.", ["pyproject.toml"], True, True, 1)
    ).is_correct
