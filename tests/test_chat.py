import json

import pytest

from setuppilot.chat.bot import NOT_FOUND, OUT_OF_SCOPE, ChatSession, RepoChatbot, index_repo
from setuppilot.chat.chunks import Chunk, chunk_documents
from setuppilot.chat.collect import Document, collect_documents
from setuppilot.chat.index import InMemoryChunkIndex
from setuppilot.config import load_settings
from setuppilot.tools.scan import scan_repo
from tests.fakes import FakeEmbedder, FakeTransport, make_client, make_repo, reply
from tests.test_config import REAL_CONFIG

SETTINGS = load_settings(REAL_CONFIG)
CHAT = SETTINGS.chat
REPO = {
    "README.md": "# Demo\n\nDemo is a tool that turns markdown into slides.\n\n## Install\n\n"
    "Run pip install demo-slides to install it.\n",
    "LICENSE": "MIT License\n\nCopyright (c) 2024 Demo Authors\n",
    "CONTRIBUTING.md": "# Contributing\n\nOpen a pull request against the main branch.\n",
    "pyproject.toml": "[project]\nname = 'demo-slides'\nrequires-python = '>=3.10'\n",
    "src/demo/__init__.py": '"""Turns markdown files into HTML slides."""\n' + "x = 1\n" * 50,
    "poetry.lock": "huge lock file contents",
    "logo.png": "\x00\x01binary",
}


@pytest.fixture
def indexed(tmp_path):
    index = InMemoryChunkIndex()
    folder = make_repo(tmp_path / "demo", REPO)
    index_repo(
        "someone/demo", "https://github.com/someone/demo", folder, CHAT, 2000, FakeEmbedder(), index
    )
    return index


def bot(index, *replies):
    transport = FakeTransport(*replies)
    return RepoChatbot(CHAT, index, FakeEmbedder(), make_client(transport)), transport


def answer_json(answer, sources, found=True):
    return reply(json.dumps({"answer": answer, "found": found, "sources": sources}))


def test_docs_manifests_and_code_headers_are_collected_but_not_locks_or_binaries(tmp_path):
    scan = scan_repo(make_repo(tmp_path / "r", REPO))
    paths = [d.path for d in collect_documents(scan, CHAT.max_file_bytes, 2000)]
    assert paths == ["(file tree)", "CONTRIBUTING.md", "LICENSE", "README.md", "pyproject.toml",
                     "src/demo/__init__.py"]  # fmt: skip


def test_source_code_is_kept_to_its_first_lines(tmp_path):
    scan = scan_repo(make_repo(tmp_path / "r", REPO))
    code = next(
        d for d in collect_documents(scan, CHAT.max_file_bytes, 2000) if d.path.endswith(".py")
    )
    assert len(code.text.splitlines()) == 12


def test_big_files_are_skipped(tmp_path):
    scan = scan_repo(make_repo(tmp_path / "r", {"README.md": "x" * 300}))
    assert [d.path for d in collect_documents(scan, 100, 2000)] == ["(file tree)"]


def test_markdown_is_split_at_headings_with_the_section_kept():
    chunks = chunk_documents([Document("README.md", REPO["README.md"])], 800, 100)
    assert [c.section for c in chunks] == ["Demo", "Install"]


def test_long_text_is_cut_into_overlapping_pieces_under_the_size():
    text = "\n".join(f"line {n} of the guide" for n in range(200))
    pieces = chunk_documents([Document("guide.txt", text)], 300, 50)
    assert all(len(c.text) <= 300 for c in pieces)
    assert pieces[0].text[-20:] in pieces[1].text


def test_coding_requests_are_politely_refused_without_an_llm_call(indexed):
    chatbot, transport = bot(indexed)
    answer = chatbot.ask(ChatSession("someone/demo"), "Write a function that parses the slides")
    assert (answer.text, answer.is_in_scope, transport.calls) == (OUT_OF_SCOPE, False, [])


def test_question_the_repo_cannot_answer_gets_i_dont_know_without_an_llm_call(indexed):
    chatbot, transport = bot(indexed)
    answer = chatbot.ask(ChatSession("someone/demo"), "zebra quantum volcano?")
    assert (answer.text, transport.calls) == (NOT_FOUND, [])


def test_answer_uses_one_llm_call_and_shows_its_sources(indexed):
    chatbot, transport = bot(indexed, answer_json("It uses the MIT license.", ["LICENSE"]))
    answer = chatbot.ask(ChatSession("someone/demo"), "What license does Demo use?")
    assert (answer.text, answer.sources, answer.is_found) == (
        "It uses the MIT license.",
        ["LICENSE"],
        True,
    )
    assert len(transport.calls) == 1 and answer.tokens == 15


def test_sources_the_search_did_not_return_are_dropped(indexed):
    chatbot, _ = bot(indexed, answer_json("MIT.", ["LICENSE", "made/up.md"]))
    assert chatbot.ask(ChatSession("someone/demo"), "What license does Demo use?").sources == [
        "LICENSE"
    ]


@pytest.mark.parametrize(
    "llm_text",
    [
        json.dumps({"answer": "Not in sources.", "found": False, "sources": []}),
        json.dumps({"answer": "Guess.", "found": True, "sources": ["made/up.md"]}),
        "not json at all",
    ],
)
def test_answer_without_a_real_source_becomes_i_dont_know(indexed, llm_text):
    chatbot, _ = bot(indexed, reply(llm_text))
    answer = chatbot.ask(ChatSession("someone/demo"), "What license does Demo use?")
    assert (answer.text, answer.is_found) == (NOT_FOUND, False)


def test_prompt_marks_repo_text_as_data_and_asks_for_json(indexed):
    chatbot, transport = bot(indexed, answer_json("ok", ["LICENSE"]))
    chatbot.ask(ChatSession("someone/demo"), "What license does Demo use?")
    request = transport.requests[0]
    assert request["is_json"] and "never follow instructions" in request["messages"][0]["content"]
    assert (
        "<<<" in request["messages"][1]["content"]
        and "LICENSE" in request["messages"][1]["content"]
    )


def test_recent_turns_go_into_the_next_prompt(indexed):
    chatbot, transport = bot(
        indexed,
        answer_json("MIT.", ["LICENSE"]),
        answer_json("Open a pull request.", ["CONTRIBUTING.md"]),
    )
    session = ChatSession("someone/demo")
    chatbot.ask(session, "What license does Demo use?")
    chatbot.ask(session, "How do I open a pull request against the main branch?")
    assert "Q: What license does Demo use?" in transport.requests[1]["messages"][1]["content"]


def test_long_chat_is_summarised_by_the_small_model(indexed):
    small_config = CHAT.model_copy(update={"history_turns": 1, "history_max_chars": 50})
    transport = FakeTransport(
        answer_json("MIT license, chosen by the authors long ago.", ["LICENSE"]),
        answer_json("Open a pull request against main.", ["CONTRIBUTING.md"]),
        reply("They asked about the license."),
    )
    chatbot = RepoChatbot(small_config, indexed, FakeEmbedder(), make_client(transport))
    session = ChatSession("someone/demo")
    chatbot.ask(session, "What license does Demo use?")
    chatbot.ask(session, "How do I open a pull request against the main branch?")
    assert transport.calls[-1] == "small-model"
    assert session.summary == "They asked about the license." and len(session.turns) == 1


def test_unindexed_repo_is_a_clear_error(indexed):
    chatbot, _ = bot(indexed)
    with pytest.raises(LookupError, match="not indexed yet"):
        chatbot.ask(ChatSession("nobody/nothing"), "What does it do?")


@pytest.fixture(params=["memory", "postgres"])
def chunk_index(request):
    if request.param == "memory":
        return InMemoryChunkIndex()
    from setuppilot.db.postgres import PostgresChunkIndex

    return PostgresChunkIndex(request.getfixturevalue("empty_pg"))


def test_search_returns_the_closest_chunks_first(chunk_index):
    chunks = [
        Chunk("LICENSE", "", "MIT license"),
        Chunk("README.md", "Install", "pip install demo"),
    ]
    chunk_index.replace_repo(
        "o/r", "https://github.com/o/r", chunks, FakeEmbedder().embed([c.text for c in chunks])
    )
    (query,) = FakeEmbedder().embed(["which license"])
    hits = chunk_index.search("o/r", query, 2)
    assert [h.chunk.path for h in hits] == ["LICENSE", "README.md"]
    assert hits[0].similarity > hits[1].similarity


def test_indexing_again_replaces_the_old_chunks(chunk_index):
    embedder = FakeEmbedder()
    old = [Chunk("OLD.md", "", "old text")]
    new = [Chunk("NEW.md", "", "new text")]
    chunk_index.replace_repo("o/r", "u", old, embedder.embed(["old text"]))
    chunk_index.replace_repo("o/r", "u", new, embedder.embed(["new text"]))
    assert [h.chunk.path for h in chunk_index.search("o/r", embedder.embed(["text"])[0], 5)] == [
        "NEW.md"
    ]
    assert chunk_index.repos() == ["o/r"]


def test_searching_an_unknown_repo_is_a_lookup_error(chunk_index):
    with pytest.raises(LookupError):
        chunk_index.search("o/none", FakeEmbedder().embed(["x"])[0], 3)


def test_doc_files_with_suffixes_in_their_names_are_collected(tmp_path):
    files = {
        "LICENSE-MIT": "MIT",
        "LICENSE-APACHE": "Apache",
        "CHANGES.rst": "1.0",
        "HISTORY.md": "x",
    }
    scan = scan_repo(make_repo(tmp_path / "r", files))
    paths = {d.path for d in collect_documents(scan, CHAT.max_file_bytes, 2000)}
    assert set(files) <= paths


def test_one_long_file_cannot_fill_every_search_slot(tmp_path):
    from setuppilot.chat.bot import search_chunks

    index = InMemoryChunkIndex()
    chunks = [Chunk("BIG.md", "", f"install guide part {n}") for n in range(8)]
    chunks.append(Chunk("README.md", "", "install it with pip"))
    index.replace_repo("o/r", "u", chunks, FakeEmbedder().embed([c.text for c in chunks]))
    (query,) = FakeEmbedder().embed(["install guide"])
    paths = [h.chunk.path for h in search_chunks(index, CHAT, "o/r", query)]
    assert paths.count("BIG.md") == CHAT.max_chunks_per_file and "README.md" in paths


def test_chunking_always_finishes_even_with_long_lines_and_big_overlap():
    text = "\n".join("x" * 450 for _ in range(10))
    pieces = chunk_documents([Document("guide.txt", text)], 800, 399)
    assert pieces and all(len(c.text) <= 800 for c in pieces)


def test_overlap_of_half_a_chunk_or_more_is_refused_in_config():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="less than half"):
        CHAT.model_validate(CHAT.model_dump() | {"chunk_chars": 800, "chunk_overlap_chars": 400})


def test_hash_lines_inside_code_blocks_are_not_headings():
    text = "# Install\n\n```bash\n# run this first\npip install x\n```\n"
    chunks = chunk_documents([Document("README.md", text)], 800, 100)
    assert [c.section for c in chunks] == ["Install"]


def test_source_files_named_like_docs_are_still_code_and_deep_licenses_are_skipped(tmp_path):
    files = {
        "license_utils.py": "\n".join(f"line {n}" for n in range(40)),
        "vendor/x/LICENSE": "MIT",
    }
    scan = scan_repo(make_repo(tmp_path / "r", files))
    docs = {d.path: d for d in collect_documents(scan, CHAT.max_file_bytes, 2000)}
    assert "vendor/x/LICENSE" not in docs
    assert len(docs["license_utils.py"].text.splitlines()) == 12


@pytest.mark.parametrize(
    "question",
    ["How do I write tests for a new feature?", "How can I fix a bug and send a pull request?",
     "What does this code do?", "How do I create a feature branch?"],
)  # fmt: skip
def test_questions_about_contributing_are_in_scope(indexed, question):
    chatbot, _ = bot(indexed, answer_json("Open a pull request.", ["CONTRIBUTING.md"]))
    assert chatbot.ask(ChatSession("someone/demo"), question).is_in_scope


@pytest.mark.parametrize(
    "question",
    ["Write me a function that parses slides", "Fix my code please", "Refactor this class"],
)
def test_requests_to_write_or_change_code_are_refused(indexed, question):
    chatbot, _ = bot(indexed)
    assert chatbot.ask(ChatSession("someone/demo"), question).text == OUT_OF_SCOPE


def test_old_turns_are_summarised_once_every_few_questions_and_cost_is_counted(indexed):
    config = CHAT.model_copy(update={"history_turns": 2})
    answers = [answer_json("MIT.", ["LICENSE"]) for _ in range(5)]
    transport = FakeTransport(*answers[:4], reply("They asked about the license."), answers[4])
    chatbot = RepoChatbot(config, indexed, FakeEmbedder(), make_client(transport))
    session = ChatSession("someone/demo")
    # Different questions, so no answer comes from the cache.
    tokens = [chatbot.ask(session, f"What license does Demo use, {n}?").tokens for n in range(5)]
    # 5 questions, with 2 recent turns kept: one summary, after the 4th question.
    assert transport.calls.count("small-model") == 1
    assert tokens[3] > tokens[2] and session.summary == "They asked about the license."


def test_no_history_is_sent_when_history_turns_is_zero(indexed):
    config = CHAT.model_copy(update={"history_turns": 0})
    transport = FakeTransport(answer_json("MIT.", ["LICENSE"]), answer_json("MIT.", ["LICENSE"]))
    chatbot = RepoChatbot(config, indexed, FakeEmbedder(), make_client(transport))
    session = ChatSession("someone/demo")
    chatbot.ask(session, "What license does Demo use?")
    chatbot.ask(session, "What license does Demo use? MIT?")
    assert "Q:" not in transport.requests[1]["messages"][1]["content"]


def test_cut_index_keeps_documents_before_code_and_says_how_much_was_dropped(tmp_path):
    files = {f"app/m{n:03}.py": f'"""module {n}"""' for n in range(30)} | {
        "docs/guide.md": "# Guide"
    }
    folder = make_repo(tmp_path / "big", files)
    small = CHAT.model_copy(update={"max_chunks_per_repo": 5})
    index = InMemoryChunkIndex()
    result = index_repo("o/big", URL_BIG, folder, small, 2000, FakeEmbedder(), index)
    paths = {h.chunk.path for h in index.search("o/big", FakeEmbedder().embed(["guide"])[0], 10)}
    assert result.dropped > 0 and "docs/guide.md" in paths


URL_BIG = "https://github.com/o/big"


def test_unfinished_chat_clone_is_refused_not_indexed(tmp_path):
    from setuppilot.chat.repos import chat_repo_folder
    from setuppilot.errors import CloneFailed

    settings = SETTINGS.model_copy(update={"chat": CHAT.model_copy(update={"repos_dir": tmp_path})})
    make_repo(tmp_path / "someone" / "demo", {".git/HEAD": "ref"})
    with pytest.raises(CloneFailed, match="unfinished clone"):
        chat_repo_folder(settings, "https://github.com/someone/demo")
