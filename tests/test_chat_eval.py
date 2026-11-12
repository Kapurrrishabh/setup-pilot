import json
from pathlib import Path

import pytest

from setuppilot.chat.bot import NOT_FOUND, ChatAnswer, Citation, Retrieval, index_repo
from setuppilot.chat.chunks import Chunk
from setuppilot.chat.index import ChatRepo, IndexedRepo, InMemoryChunkIndex
from setuppilot.config import load_settings
from setuppilot.db.metrics import InMemoryMetricsStore
from setuppilot.db.runs import InMemoryRunStore
from setuppilot.db.stores import Stores
from setuppilot.errors import LLMUnavailable
from setuppilot.evaluation.chat_eval import (
    ChatCase,
    ResultsOutdated,
    load_chat_cases,
    load_done,
    results_table,
    score_answer,
    score_retrieval,
)
from setuppilot.evaluation.run_chat import (
    CacheMiss,
    CacheOnlyTransport,
    replay_client,
    run_chat_eval,
)
from setuppilot.llm.cache import InMemoryCache
from setuppilot.llm.usage import InMemoryUsageStore
from tests.fakes import (
    FAKE_COMMIT,
    FakeEmbedder,
    FakeTransport,
    groq_config,
    make_chat_clone,
    make_client,
    make_repo,
    reply,
    reply_citing,
)
from tests.test_config import REAL_CONFIG

SETTINGS = load_settings(REAL_CONFIG)
URL = "https://github.com/someone/demo"
REPO = ChatRepo("someone/demo", URL, FAKE_COMMIT, owner_github_id=None)


def case(question="What license is it under?", kind="base", facts=("mit",), sources=("LICENSE",),
         follows=None, is_substring_match=False):  # fmt: skip
    return ChatCase(URL, question, kind, facts, sources, follows, is_substring_match)


LICENSE_CASE = case()
UNKNOWN_CASE = case("What is the company's revenue?", "unanswerable", (), ())
LICENSE_CHUNK = Chunk("LICENSE", "", "MIT License text", 1, 1, "doc")


def answer(text, paths=(), status="answered"):
    citations = [Citation(n, p, 1, 2, None, "") for n, p in enumerate(paths, start=1)]
    return ChatAnswer(status, text, citations, [], 9)


def found(*hits, is_gated=False, card=()):
    return Retrieval(IndexedRepo(REPO, "m", tuple(card)), list(hits), [], 0.5, 0.0, is_gated)


def test_correct_and_grounded_answer_scores_both():
    score = score_answer(LICENSE_CASE, answer("It is MIT licensed.", ["LICENSE"]))
    assert (score.is_correct, score.is_grounded) == (True, True)


def test_right_words_from_the_wrong_file_are_not_grounded():
    score = score_answer(LICENSE_CASE, answer("MIT.", ["README.md"]))
    assert (score.is_correct, score.is_grounded) == (True, False)


def test_any_one_of_the_expected_files_counts_as_grounded():
    either = case(sources=("LICENSE", "package.json"))
    assert score_answer(either, answer("MIT.", ["package.json"])).is_grounded


def test_i_dont_know_is_right_only_for_questions_the_repo_cannot_answer():
    idk = answer(NOT_FOUND, status="not_found")
    scores = [score_answer(c, idk).is_correct for c in (UNKNOWN_CASE, LICENSE_CASE)]
    assert scores == [True, False]


def test_retrieval_checks_the_file_found_and_the_facts_sent():
    score = score_retrieval(LICENSE_CASE, found(LICENSE_CHUNK))
    assert (score.is_correct, score.is_grounded) == (True, True)


def test_retrieval_finds_facts_on_the_repo_card_too():
    card = [Chunk("pyproject.toml", "repo card", "license: MIT", 0, 0, "card")]
    readme = Chunk("README.md", "", "A tool.", 1, 1, "doc")
    assert score_retrieval(LICENSE_CASE, found(readme, card=card)).is_correct


def test_retrieval_stopped_by_the_gate_is_a_miss():
    score = score_retrieval(LICENSE_CASE, found(LICENSE_CHUNK, is_gated=True))
    assert (score.is_correct, score.is_grounded) == (False, False)


def test_retrieval_does_not_score_i_dont_know():
    score = score_retrieval(UNKNOWN_CASE, found())
    assert (score.is_correct, score.is_grounded) == (None, None)


def test_substring_match_finds_code_facts_inside_longer_names():
    code = case("Which hash?", "code", ("sha1",), ("signer.py",), is_substring_match=True)
    assert score_answer(code, answer("It uses hashlib.sha1.", ["signer.py"])).is_correct


def test_facts_must_be_whole_words():
    wrong = score_answer(LICENSE_CASE, answer("Use is permitted freely.", ["LICENSE"]))
    right = score_answer(LICENSE_CASE, answer("It is MIT.", ["LICENSE"]))
    assert (wrong.is_correct, right.is_correct) == (False, True)


@pytest.mark.parametrize(
    ("facts", "text"),
    [(("integer", "string"), "It converts integer primitives to decimal strings."),
     (("sign",), "Data is cryptographically signed, so it cannot be changed.")],
)  # fmt: skip
def test_facts_match_the_plain_word_endings_the_readmes_use(facts, text):
    assert score_answer(case(facts=facts), answer(text, ["LICENSE"])).is_correct


def test_version_facts_do_not_match_longer_versions():
    version = case(facts=("1.0",), sources=("pyproject.toml",))
    assert not score_answer(version, answer("It is 1.0.18.", ["pyproject.toml"])).is_correct


def write_questions(tmp_path, question):
    path = tmp_path / "q.yaml"
    path.write_text(json.dumps({"repos": [{"url": URL, "questions": [question]}]}))
    return path


@pytest.mark.parametrize(
    ("question", "problem"),
    [({"question": "q", "kind": "base"}, "needs expected_facts"),
     ({"question": "q", "kind": "chat"}, "has kind 'chat'"),
     ({"question": "q", "kind": "followup", "expected_facts": ["a"], "expected_sources": ["b"]},
      "needs follows"),
     ({"question": "q", "kind": "offtopic", "expected_facts": ["a"]}, "must not list facts"),
     ({"question": "q", "kind": "code", "expected_facts": ["a"], "expected_sources": ["b"],
       "match": "regex"}, "use word or substring")],
)  # fmt: skip
def test_question_file_mistakes_are_refused(tmp_path, question, problem):
    with pytest.raises(ValueError, match=problem):
        load_chat_cases(write_questions(tmp_path, question))


def test_question_set_has_enough_code_follow_up_and_off_topic_questions():
    cases = load_chat_cases(Path("eval/chat_questions.yaml"))
    counts = {kind: sum(c.kind == kind for c in cases) for kind in ("code", "followup", "offtopic")}
    assert counts["code"] >= 25 and counts["followup"] >= 10 and counts["offtopic"] >= 5


def test_table_has_a_row_per_repo_a_total_and_a_row_per_kind():
    scores = [
        score_answer(LICENSE_CASE, answer("MIT", ["LICENSE"])),
        score_answer(UNKNOWN_CASE, answer(NOT_FOUND, status="not_found")),
    ]
    table = results_table(scores)
    assert f"| {URL} | 2 | 1/1 (100%) | 1/1 (100%) | 1/1 (100%) | 9 |" in table
    assert "| **Total** |" in table and "| unanswerable | 1 | - | - | 1/1 (100%) |" in table


def test_old_progress_file_is_a_clear_error(tmp_path):
    old = {"repo_url": URL, "question": "q", "is_answerable": True, "answer": "a", "sources": [],
           "is_correct": True, "is_grounded": True, "tokens": 1}  # fmt: skip
    path = tmp_path / "chat-retrieval.jsonl"
    path.write_text(json.dumps(old) + "\n")
    with pytest.raises(ResultsOutdated, match="older version"):
        load_done(path)


def stores_with(index):
    return Stores(
        InMemoryRunStore(), InMemoryUsageStore(), InMemoryCache(), InMemoryMetricsStore(),
        index, None, None,
    )  # fmt: skip


@pytest.fixture
def stores(tmp_path):
    index = InMemoryChunkIndex()
    folder = make_repo(tmp_path / "demo", {"LICENSE": "MIT License text", "README.md": "# Demo"})
    index_repo(REPO, folder, SETTINGS.chat, 2000, FakeEmbedder(), index)
    return stores_with(index)


def test_retrieval_mode_needs_no_llm(tmp_path, stores):
    scores = run_chat_eval(
        SETTINGS, stores, [LICENSE_CASE], "retrieval", tmp_path / "out", FakeEmbedder(), None
    )
    assert scores[0].is_grounded
    assert list((tmp_path / "out").glob("chat-retrieval-*.md"))


def test_follow_up_is_asked_after_its_earlier_question_in_the_same_chat(tmp_path, stores):
    follow_up = case("Is that MIT?", "followup", follows="What license is it under?")
    transport = FakeTransport(reply_citing("LICENSE", "MIT"), reply_citing("LICENSE", "MIT"))
    run_chat_eval(SETTINGS, stores, [follow_up], "full", tmp_path, FakeEmbedder(),
                  make_client(transport))  # fmt: skip
    assert "Q: What license is it under?" in transport.requests[1]["messages"][1]["content"]


def test_stopped_run_carries_on_where_it_left_off(tmp_path, stores):
    limit = LLMUnavailable("Daily limit reached.", "x")
    # The BIG model and then the SMALL one both hit the limit on the second question.
    first = make_client(FakeTransport(reply_citing("LICENSE", "MIT"), limit, limit))
    cases = [LICENSE_CASE, case("Is it MIT License text?")]
    done = run_chat_eval(SETTINGS, stores, cases, "full", tmp_path, FakeEmbedder(), first)
    assert len(done) == 1
    second = make_client(FakeTransport(reply_citing("LICENSE", "MIT")))
    assert (
        len(run_chat_eval(SETTINGS, stores, cases, "full", tmp_path, FakeEmbedder(), second)) == 2
    )


def test_a_cut_off_answer_counts_as_a_miss_and_the_evaluation_goes_on(tmp_path, stores):
    llm = make_client(FakeTransport(reply("{", is_truncated=True), reply_citing("LICENSE", "MIT")))
    cases = [LICENSE_CASE, case("Is it MIT License text?")]
    done = run_chat_eval(SETTINGS, stores, cases, "full", tmp_path, FakeEmbedder(), llm)
    assert [s.is_correct for s in done] == [False, True]


def test_a_cut_off_answer_is_not_a_correct_i_dont_know(tmp_path, stores):
    unknown = case("Who is the license holder's revenue?", "unanswerable", (), ())
    llm = make_client(FakeTransport(reply("{", is_truncated=True)))
    (score,) = run_chat_eval(SETTINGS, stores, [unknown], "full", tmp_path, FakeEmbedder(), llm)
    assert score.is_correct is False


def test_a_stop_before_any_answer_still_writes_the_table_in_a_new_folder(tmp_path, stores):
    limit = LLMUnavailable("Daily limit reached.", "x")
    llm = make_client(FakeTransport(limit, limit))
    run_chat_eval(SETTINGS, stores, [LICENSE_CASE], "full", tmp_path / "new", FakeEmbedder(), llm)
    assert list((tmp_path / "new").glob("chat-full-*.md"))


def test_replay_mode_never_calls_groq():
    with pytest.raises(CacheMiss, match="not in the cache"):
        CacheOnlyTransport().send(groq_config().big, [], 10, "json")


def test_replay_cache_miss_is_not_counted_as_a_switch_to_the_small_model(tmp_path, stores):
    llm = replay_client(SETTINGS, stores)
    run_chat_eval(SETTINGS, stores, [LICENSE_CASE], "replay", tmp_path, FakeEmbedder(), llm)
    assert stores.metrics.count("small_model_fallback") == 0


def test_rerun_says_its_scores_come_from_an_earlier_run(tmp_path, stores, capsys):
    for _ in range(2):
        run_chat_eval(SETTINGS, stores, [LICENSE_CASE], "retrieval", tmp_path, FakeEmbedder(), None)
    assert "scored by an earlier run" in capsys.readouterr().out


def test_eval_searches_a_repo_it_had_to_index_itself(tmp_path):
    chat = SETTINGS.chat.model_copy(update={"repos_dir": tmp_path / "repos"})
    make_chat_clone(tmp_path / "repos", "someone/demo", {"LICENSE": "MIT License text"})
    scores = run_chat_eval(
        SETTINGS.model_copy(update={"chat": chat}), stores_with(InMemoryChunkIndex()),
        [LICENSE_CASE], "retrieval", tmp_path / "out", FakeEmbedder(), None,
    )  # fmt: skip
    assert scores[0].is_grounded


def test_repo_indexed_with_another_embedding_model_is_indexed_again(tmp_path):
    chat = SETTINGS.chat.model_copy(update={"repos_dir": tmp_path / "repos"})
    make_chat_clone(tmp_path / "repos", "someone/demo", {"LICENSE": "MIT License text"})
    index = InMemoryChunkIndex()
    index.replace_repo(IndexedRepo(REPO, "an-old-model", ()), [], [])
    run_chat_eval(
        SETTINGS.model_copy(update={"chat": chat}), stores_with(index), [LICENSE_CASE],
        "retrieval", tmp_path / "out", FakeEmbedder(), None,
    )  # fmt: skip
    assert index.repo("someone/demo", None).embedding_model == FakeEmbedder.model_name
