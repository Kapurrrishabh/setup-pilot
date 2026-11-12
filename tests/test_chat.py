import io
import json
import logging
import subprocess
import tarfile
import time
from dataclasses import replace
from itertools import pairwise

import pytest

from setuppilot.chat import cli as chat_cli
from setuppilot.chat.bot import (
    ANSWER_SCHEMA,
    NOT_FOUND,
    OUT_OF_SCOPE,
    AnswerCutOff,
    AnswerUnreadable,
    ChatSession,
    IndexOutdated,
    RepoChatbot,
    index_repo,
    retrieve,
)
from setuppilot.chat.card import CARD_CHARS, build_repo_card
from setuppilot.chat.chunks import Chunk, chunk_document
from setuppilot.chat.collect import (
    Document,
    TarballLimits,
    collect_documents,
    documents_from_tarball,
)
from setuppilot.chat.index import ChatRepo, IndexedRepo, InMemoryChunkIndex
from setuppilot.chat.search import fuse_rankings, tokens
from setuppilot.config import load_settings
from setuppilot.errors import CloneFailed, LLMUnavailable
from setuppilot.tools.scan import scan_repo
from tests.fakes import (
    FAKE_COMMIT,
    FakeEmbedder,
    FakeTransport,
    make_chat_clone,
    make_client,
    make_repo,
    reply,
    reply_citing,
)
from tests.test_chat_eval import stores_with
from tests.test_config import REAL_CONFIG

SETTINGS = load_settings(REAL_CONFIG)
CHAT = SETTINGS.chat
KEY = "someone/demo"
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
COUNT = FakeEmbedder().count_tokens


def chat_repo(key=KEY, owner=None):
    return ChatRepo(key, f"https://github.com/{key}", FAKE_COMMIT, owner)


def index_files(index, folder, files, config=CHAT, repo=None):
    make_repo(folder, files)
    return index_repo(repo or chat_repo(), folder, config, 2000, FakeEmbedder(), index)


@pytest.fixture
def indexed(tmp_path):
    index = InMemoryChunkIndex()
    index_files(index, tmp_path / "demo", REPO)
    return index


def bot(index, *replies, config=CHAT):
    transport = FakeTransport(*replies)
    return RepoChatbot(config, index, FakeEmbedder(), make_client(transport)), transport


def session():
    return ChatSession(KEY, viewer_github_id=None)


def answer_json(answer, sources, found=True):
    return reply(json.dumps({"answer": answer, "found": found, "sources": sources}))


def prompt_of(transport, n=0):
    return transport.requests[n]["messages"][1]["content"]


def function(name, lines=40):
    body = "\n".join(f"    x{n} = {n}" for n in range(lines))
    return f"def {name}():\n{body}\n"


# Which files are read


def test_docs_manifests_and_code_are_collected_but_not_locks_or_binaries(tmp_path):
    scan = scan_repo(make_repo(tmp_path / "r", REPO))
    paths = [d.path for d in collect_documents(scan, CHAT.max_file_bytes, 2000)]
    assert paths == ["(file tree)", "CONTRIBUTING.md", "LICENSE", "README.md", "pyproject.toml",
                     "src/demo/__init__.py"]  # fmt: skip


def test_source_code_is_read_whole_not_just_its_first_lines(tmp_path):
    scan = scan_repo(make_repo(tmp_path / "r", REPO))
    code = next(
        d for d in collect_documents(scan, CHAT.max_file_bytes, 2000) if d.path.endswith(".py")
    )
    assert len(code.text.splitlines()) == 51


@pytest.mark.parametrize(
    ("path", "kind"),
    [("tests/test_app.py", "test"), ("src/app.test.js", "test"), ("pkg/run_test.go", "test"),
     ("src/app.py", "code"), (".github/workflows/ci.yml", "ci"), ("package.json", "manifest"),
     ("requirements-dev.txt", "manifest"), ("README.md", "doc"), ("docs/guide.rst", "doc")],
)  # fmt: skip
def test_each_file_gets_its_kind(tmp_path, path, kind):
    scan = scan_repo(make_repo(tmp_path / "r", {path: "some text\n"}))
    docs = collect_documents(scan, CHAT.max_file_bytes, 2000)
    assert [d.kind for d in docs if d.path == path] == [kind]


@pytest.mark.parametrize(
    ("path", "text"),
    [("static/app.min.js", "var a=1;\n"), ("vendor/lib/x.py", "x = 1\n"),
     ("third_party/y.go", "package y\n"), ("src/bundle.js", "x" * 5000 + "\n")],
    ids=["minified name", "vendor", "third_party", "generated long lines"],
)  # fmt: skip
def test_minified_vendored_and_generated_files_are_left_out(tmp_path, path, text):
    scan = scan_repo(make_repo(tmp_path / "r", {path: text}))
    assert [d.path for d in collect_documents(scan, CHAT.max_file_bytes, 2000)] == ["(file tree)"]


def test_big_files_are_skipped(tmp_path):
    scan = scan_repo(make_repo(tmp_path / "r", {"README.md": "x" * 300}))
    assert [d.path for d in collect_documents(scan, 100, 2000)] == ["(file tree)"]


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


def test_source_files_named_like_docs_are_still_code_and_deep_licenses_are_skipped(tmp_path):
    files = {"license_utils.py": "x = 1\n", "vendor/x/LICENSE": "MIT"}
    scan = scan_repo(make_repo(tmp_path / "r", files))
    docs = {d.path: d.kind for d in collect_documents(scan, CHAT.max_file_bytes, 2000)}
    assert docs == {"(file tree)": "tree", "license_utils.py": "code"}


# Reading from a GitHub tarball


def tarball(files, links=(), top="someone-demo-0123abc"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, text in files.items():
            raw = text.encode()
            member = tarfile.TarInfo(f"{top}/{name}" if top else name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        for name, target in links:
            link = tarfile.TarInfo(f"{top}/{name}")
            link.type, link.linkname = tarfile.SYMTYPE, target
            archive.addfile(link)
    return data.getvalue()


LIMITS = TarballLimits(max_file_bytes=CHAT.max_file_bytes, max_total_bytes=10**6, tree_chars=2000)


def test_tarball_gives_the_same_documents_as_a_clone(tmp_path):
    scan = scan_repo(make_repo(tmp_path / "r", REPO))
    from_clone = collect_documents(scan, CHAT.max_file_bytes, 2000)
    from_tarball = documents_from_tarball(tarball(REPO), LIMITS)
    assert sorted(from_tarball, key=lambda d: d.path) == sorted(from_clone, key=lambda d: d.path)


def test_tarball_leaves_out_links_odd_paths_and_skipped_folders():
    files = {"README.md": "# Hi\n", "a/../escape.md": "x", "node_modules/x/index.js": "y"}
    docs = documents_from_tarball(tarball(files, links=[("LICENSE", "/etc/passwd")]), LIMITS)
    assert [(d.path, d.text) for d in docs] == [
        ("(file tree)", "README.md"),
        ("README.md", "# Hi\n"),
    ]


def test_tarball_with_too_much_text_is_refused():
    limits = replace(LIMITS, max_total_bytes=100)
    with pytest.raises(CloneFailed, match="too much text"):
        documents_from_tarball(tarball({"README.md": "x" * 60, "docs/b.md": "y" * 60}), limits)


@pytest.mark.parametrize("keep", [0.1, 0.5, 0.9])
def test_cut_short_or_broken_tarball_is_a_clear_error(keep):
    data = tarball(REPO | {f"docs/page{n}.md": f"# Page {n}\n" * 50 for n in range(20)})
    with pytest.raises(CloneFailed, match="could not be read"):
        documents_from_tarball(data[: int(len(data) * keep)], LIMITS)


# Chunking


def test_markdown_is_split_at_headings_with_the_section_kept():
    chunks = chunk_document(Document("README.md", REPO["README.md"], "doc"), 254, COUNT)
    spans = [(c.section, c.start_line, c.end_line) for c in chunks]
    assert spans == [("Demo", 1, 3), ("Install", 5, 7)]


def test_rst_is_split_at_its_underlined_headings():
    text = "Title\n=====\n\nAbout it.\n\nInstalling\n----------\n\npip install demo\n"
    chunks = chunk_document(Document("docs/index.rst", text, "doc"), 254, COUNT)
    assert [c.section for c in chunks] == ["Title", "Installing"]


def test_hash_lines_inside_code_blocks_are_not_headings():
    text = "# Install\n\n```bash\n# run this first\npip install x\n```\n"
    chunks = chunk_document(Document("README.md", text, "doc"), 254, COUNT)
    assert [c.section for c in chunks] == ["Install"]


def test_python_is_split_at_its_functions_with_their_lines():
    source = '"""Tools."""\n\n' + function("first") + "\n\n@cache\n" + function("second")
    chunks = chunk_document(Document("pkg/tools.py", source, "code"), 254, COUNT)
    spans = [(c.section, c.start_line, c.end_line) for c in chunks]
    assert spans == [("def first", 1, 43), ("def second", 46, 87)]


def test_big_python_class_gets_a_chunk_per_method_and_a_decorated_first_method_is_kept():
    body = "\n".join(f"        y{n} = {n}" for n in range(35))
    source = (
        f'class Big:\n    """Doc."""\n\n    @property\n    def first(self):\n{body}\n\n'
        f"    def second(self):\n{body}\n"
    )
    chunks = chunk_document(Document("pkg/big.py", source, "code"), 254, COUNT)
    first = next(c for c in chunks if "Big.first" in c.section)
    assert (
        first.text.startswith("class Big:") and "    @property\n    def first(self):" in first.text
    )


def test_module_code_between_functions_is_kept():
    source = function("a") + '\nTABLE = {"answer": 42}\n\n' + function("b")
    chunks = chunk_document(Document("pkg/m.py", source, "code"), 254, COUNT)
    assert any('TABLE = {"answer": 42}' in c.text for c in chunks)


def test_other_languages_are_split_where_definitions_start():
    body = "\n".join(f"  total += {n};" for n in range(30))
    source = (
        f"function isOriginAllowed() {{\n{body}\n}}\n\nfunction configureMaxAge() {{\n{body}\n}}\n"
    )
    chunks = chunk_document(Document("lib/index.js", source, "code"), 254, COUNT)
    assert [c.section for c in chunks] == ["function isOriginAllowed() {",
                                           "function configureMaxAge() {"]  # fmt: skip


def test_the_comment_right_above_a_function_is_kept_with_it():
    body = "\n".join(f"  total += {n};" for n in range(30))
    source = (
        f"function first() {{\n{body}\n}}\n\n// Why: split is faster than a regex here.\n"
        f"function second() {{\n{body}\n}}\n"
    )
    chunks = chunk_document(Document("lib/index.js", source, "code"), 254, COUNT)
    second = next(c for c in chunks if c.section.startswith("function second"))
    assert second.text.startswith("// Why: split")


def test_small_definitions_joined_into_one_chunk_are_not_cut_apart_again():
    source = "".join(f"function helper{n}() {{\n  return {n};\n}}\n" for n in range(60))
    chunks = chunk_document(Document("lib/index.js", source, "code"), 254, COUNT)
    # A joined span cut again would repeat its last lines in a small tail chunk.
    assert all(a.end_line < b.start_line for a, b in pairwise(chunks))


def test_code_without_definitions_is_one_chunk():
    chunks = chunk_document(Document("run.sh", "set -e\necho hi\n", "code"), 254, COUNT)
    assert [(c.text, c.start_line, c.end_line) for c in chunks] == [("set -e\necho hi", 1, 2)]


@pytest.mark.parametrize(
    "text",
    ["\n".join(f"line {n} of the guide, with a few more words" for n in range(300)),
     " ".join(f"word{n}" for n in range(2000)), "x" * 5000],
    ids=["many lines", "one long line", "one long word"],
)  # fmt: skip
def test_every_chunk_fits_the_embedding_window(text):
    chunks = chunk_document(Document("guide.txt", text, "doc"), 60, COUNT)
    assert chunks and all(COUNT(c.search_text) <= 60 for c in chunks)


def test_a_line_longer_than_the_window_is_split_not_cut_off():
    words = [f"word{n}" for n in range(2000)]
    chunks = chunk_document(Document("guide.txt", " ".join(words), "doc"), 60, COUNT)
    assert " ".join(c.text for c in chunks).split() == words


def test_long_text_is_cut_into_overlapping_pieces():
    text = "\n".join(f"line {n} of the guide" for n in range(200))
    pieces = chunk_document(Document("guide.txt", text, "doc"), 60, COUNT)
    assert pieces[0].text.splitlines()[-1] == pieces[1].text.splitlines()[1]


def test_line_numbers_count_only_real_line_breaks():
    # A form feed is no line break on GitHub, so the link must not be one line off.
    text = "# A\ntext\x0cmore\n# B\nb\n"
    chunks = chunk_document(Document("README.md", text, "doc"), 254, COUNT)
    assert [(c.section, c.start_line) for c in chunks] == [("A", 1), ("B", 3)]


def test_the_file_tree_has_no_line_numbers():
    chunks = chunk_document(Document("(file tree)", "README.md\nsrc/a.py", "tree"), 254, COUNT)
    assert [(c.start_line, c.end_line) for c in chunks] == [(0, 0)]


# The repo card


@pytest.mark.parametrize(
    ("path", "text", "fact"),
    [("pyproject.toml", "[project]\nrequires-python = '>=3.10'\n", "requires-python: >=3.10"),
     ("pyproject.toml", "[project]\nclassifiers = ['License :: OSI Approved :: MIT License']\n",
      "license: MIT License"),
     ("package.json", '{"engines": {"node": ">=20"}}', "node engines: >=20"),
     ("Cargo.toml", '[package]\nrust-version = "1.68"\n', "rust-version: 1.68"),
     (".github/workflows/test.yml",
      "jobs:\n  test:\n    strategy:\n      matrix:\n        os: [ubuntu-latest, windows-latest]\n",
      "CI job test: os: ubuntu-latest, windows-latest")],
)  # fmt: skip
def test_card_holds_facts_from_manifests_and_ci_under_their_file(path, text, fact):
    (card,) = build_repo_card([Document(path, text, "manifest")])
    assert (card.path, card.kind, fact in card.text.splitlines()) == (path, "card", True)


def test_broken_manifest_gives_no_card_but_no_error():
    assert build_repo_card([Document("pyproject.toml", "[project\nname=", "manifest")]) == []


def test_a_broken_workflow_is_logged_without_its_text(caplog):
    text = "jobs:\n  test:\n    token: SECRET_VALUE_abc123: x\n"
    with caplog.at_level(logging.INFO):
        assert build_repo_card([Document(".github/workflows/ci.yml", text, "ci")]) == []
    (record,) = caplog.records
    assert "SECRET_VALUE" not in json.dumps(record.__dict__, default=str)


def test_card_stays_short_even_when_yaml_aliases_repeat_a_long_value():
    uses = ", ".join(["*big"] * 5000)
    text = (
        f"big: &big {'a' * 1000}\njobs:\n  t:\n    strategy:\n      matrix:\n        os: [{uses}]\n"
    )
    started = time.monotonic()
    card = build_repo_card([Document(".github/workflows/x.yml", text, "ci")])
    assert sum(len(c.text) for c in card) <= CARD_CHARS and time.monotonic() - started < 1


def test_card_is_sent_with_every_question(indexed):
    chatbot, transport = bot(indexed, reply_citing("CONTRIBUTING.md"))
    chatbot.ask(session(), "How do I open a pull request against the main branch?")
    assert "pyproject.toml > repo card\n<<<\nname: demo-slides" in prompt_of(transport)


# Search


def test_words_split_camel_and_snake_case_and_keep_versions_whole():
    assert tokens("isOriginAllowed DECIMAL_PAIRS requires-python 3.10") == [
        "origin", "allow", "decimal", "pair", "requir", "python", "3.10"
    ]  # fmt: skip


def test_an_item_both_rankings_like_beats_one_only_the_first_likes():
    assert fuse_rankings([["a", "b"], ["b"]]) == ["b", "a"]


@pytest.fixture(params=["memory", "postgres"])
def chunk_index(request):
    if request.param == "memory":
        return InMemoryChunkIndex()
    from setuppilot.db.postgres import PostgresChunkIndex

    return PostgresChunkIndex(request.getfixturevalue("empty_pg"))


def save(chunk_index, chunks, repo=None, card=()):
    vectors = FakeEmbedder().embed([c.search_text for c in chunks])
    indexed = IndexedRepo(repo or chat_repo(), FakeEmbedder.model_name, tuple(card))
    chunk_index.replace_repo(indexed, chunks, vectors)


def search(chunk_index, question, viewer=None, key=KEY, limit=5):
    (vector,) = FakeEmbedder().embed([question])
    return chunk_index.search(key, viewer, vector, tokens(question), limit)


LICENSE_CHUNK = Chunk("LICENSE", "", "MIT license", 1, 1, "doc")
INSTALL_CHUNK = Chunk("README.md", "Install", "pip install demo", 5, 7, "doc")
CODE_CHUNK = Chunk("lib/index.js", "function isOriginAllowed", "if (isOriginAllowed(o)) {}", 19, 34,
                   "code")  # fmt: skip


def test_search_returns_the_closest_chunks_first(chunk_index):
    save(chunk_index, [LICENSE_CHUNK, INSTALL_CHUNK])
    hits = search(chunk_index, "which license").by_vector
    assert [h.chunk for h in hits] == [LICENSE_CHUNK, INSTALL_CHUNK]


def test_word_search_finds_an_exact_identifier(chunk_index):
    save(chunk_index, [LICENSE_CHUNK, INSTALL_CHUNK, CODE_CHUNK])
    hits = search(chunk_index, "What does isOriginAllowed return?").by_words
    assert [h.chunk for h in hits] == [CODE_CHUNK]


def test_word_search_without_a_shared_word_finds_nothing(chunk_index):
    save(chunk_index, [LICENSE_CHUNK, INSTALL_CHUNK])
    assert search(chunk_index, "zebra quantum volcano").by_words == []


def test_indexing_again_replaces_the_old_chunks(chunk_index):
    save(chunk_index, [Chunk("OLD.md", "", "old text", 1, 1, "doc")])
    save(chunk_index, [Chunk("NEW.md", "", "new text", 1, 1, "doc")])
    paths = [h.chunk.path for h in search(chunk_index, "text").by_vector]
    assert (paths, chunk_index.repos(None)) == (["NEW.md"], [KEY])


def test_repo_keeps_its_commit_embedding_model_and_card(chunk_index):
    card = (Chunk("pyproject.toml", "repo card", "license: MIT", 0, 0, "card"),)
    save(chunk_index, [LICENSE_CHUNK], card=card)
    assert chunk_index.repo(KEY, None) == IndexedRepo(chat_repo(), FakeEmbedder.model_name, card)


def test_searching_an_unknown_repo_is_a_lookup_error(chunk_index):
    with pytest.raises(LookupError):
        search(chunk_index, "x", key="o/none")


@pytest.mark.parametrize(("viewer", "is_seen"), [(7, True), (8, False), (None, False)],
                         ids=["owner", "another user", "signed out"])  # fmt: skip
def test_private_repo_is_seen_only_by_its_owner(chunk_index, viewer, is_seen):
    save(chunk_index, [LICENSE_CHUNK], repo=chat_repo(owner=7))
    assert (KEY in chunk_index.repos(viewer)) == is_seen


@pytest.mark.parametrize("viewer", [8, None], ids=["another user", "signed out"])
def test_private_repo_cannot_be_searched_by_anyone_else(chunk_index, viewer):
    save(chunk_index, [LICENSE_CHUNK], repo=chat_repo(owner=7))
    with pytest.raises(LookupError, match="not indexed yet"):
        search(chunk_index, "license", viewer=viewer)


def test_one_long_file_cannot_fill_every_search_slot(tmp_path):
    index = InMemoryChunkIndex()
    big = "\n\n".join(f"# Step {n}\n\ninstall guide step {n}" for n in range(8))
    index_files(index, tmp_path / "r", {"docs/BIG.md": big, "README.md": "install it"})
    found = retrieve(index, FakeEmbedder(), CHAT, session(), "install guide")
    paths = [c.path for c in found.hits]
    assert paths.count("docs/BIG.md") == CHAT.max_chunks_per_file and "README.md" in paths


def test_cut_index_keeps_docs_first_and_tests_last_and_says_how_many_files_were_left_out(
    tmp_path,
):
    files = {f"app/m{n:03}.py": function(f"f{n}", 10) for n in range(30)}
    files |= {f"tests/test_m{n:03}.py": function(f"test_{n}", 80) for n in range(30)}
    index = InMemoryChunkIndex()
    small = CHAT.model_copy(update={"max_chunks_per_repo": 20})
    result = index_files(index, tmp_path / "big", files | {"docs/guide.md": "# Guide"}, small)
    kept = {h.chunk.path for h in search(index, "guide def", limit=100).by_vector}
    tops = {path.split("/")[0] for path in kept}
    # 62 files: the file tree, the guide, 30 code files of one chunk and 30 tests of two.
    assert (result.dropped, tops) == (62 - len(kept), {"(file tree)", "docs", "app"})


def test_files_past_the_chunk_limit_are_never_chunked(tmp_path):
    counted = []

    class CountingEmbedder(FakeEmbedder):
        def count_tokens(self, text):
            counted.append(text)
            return super().count_tokens(text)

    files = {f"app/m{n:03}.py": function(f"f{n}", 10) for n in range(300)}
    small = CHAT.model_copy(update={"max_chunks_per_repo": 20})
    folder = make_repo(tmp_path / "big", files)
    index_repo(chat_repo(), folder, small, 2000, CountingEmbedder(), InMemoryChunkIndex())
    assert not any("def f299" in text for text in counted)


# The bot


def test_answer_uses_one_llm_call_and_cites_its_source(indexed):
    chatbot, transport = bot(indexed, reply_citing("LICENSE", "It uses the MIT license [2]."))
    answer = chatbot.ask(session(), "What license does Demo use?")
    assert (answer.status, answer.text, [c.path for c in answer.citations]) == (
        "answered", "It uses the MIT license [2].", ["LICENSE"]
    )  # fmt: skip
    assert len(transport.calls) == 1 and answer.tokens == 15


def test_citations_link_to_the_lines_at_the_indexed_commit(indexed):
    chatbot, _ = bot(indexed, reply_citing("LICENSE"))
    (citation,) = chatbot.ask(session(), "What license does Demo use?").citations
    assert (citation.start_line, citation.end_line, citation.url) == (
        1, 3, f"https://github.com/{KEY}/blob/{FAKE_COMMIT}/LICENSE#L1-L3"
    )  # fmt: skip


def test_a_long_source_is_quoted_up_to_1200_characters(tmp_path):
    index = InMemoryChunkIndex()
    # Few tokens but many characters, as in a file of long words.
    index_files(index, tmp_path / "r", {"LICENSE": "MIT License\n" + ("x" * 600 + "\n") * 3})
    chatbot, _ = bot(index, reply_citing("LICENSE"))
    (citation,) = chatbot.ask(session(), "What license does Demo use?").citations
    assert len(citation.quote) == 1200


def test_source_numbers_that_were_not_given_are_dropped(indexed):
    chatbot, _ = bot(indexed, answer_json("MIT [1] [99].", [1, 99, 0, -1]))
    answer = chatbot.ask(session(), "What license does Demo use?")
    assert [c.n for c in answer.citations] == [1]


def test_found_without_a_real_source_becomes_i_could_not_find(indexed):
    chatbot, _ = bot(indexed, answer_json("A guess.", [99]))
    answer = chatbot.ask(session(), "What license does Demo use?")
    assert (answer.status, answer.text) == ("not_found", NOT_FOUND)


def test_not_found_says_what_is_missing_and_where_it_looked(indexed):
    chatbot, _ = bot(indexed, answer_json("The files do not say who uses it.", [], found=False))
    answer = chatbot.ask(session(), "Which companies use Demo for slides?")
    assert (answer.status, answer.text, bool(answer.looked_at)) == (
        "not_found", "The files do not say who uses it.", True
    )  # fmt: skip


@pytest.mark.parametrize("text", ["not json at all", '{"answer": 1, "found": true}'])
def test_unreadable_answer_is_an_error_not_i_could_not_find(indexed, text):
    chatbot, _ = bot(indexed, reply(text))
    with pytest.raises(AnswerUnreadable, match="could not be read"):
        chatbot.ask(session(), "What license does Demo use?")


def test_cut_off_answer_is_an_error_not_i_dont_know(indexed):
    chatbot, _ = bot(indexed, reply('{"answer": "It uses the MI', is_truncated=True))
    with pytest.raises(AnswerCutOff, match="cut off"):
        chatbot.ask(session(), "What license does Demo use?")


def test_answers_must_follow_the_strict_answer_schema(indexed):
    chatbot, transport = bot(indexed, reply_citing("LICENSE"))
    chatbot.ask(session(), "What license does Demo use?")
    assert transport.requests[0]["response_format"] == ANSWER_SCHEMA


def test_prompt_marks_repo_text_as_data(indexed):
    chatbot, transport = bot(indexed, reply_citing("LICENSE"))
    chatbot.ask(session(), "What license does Demo use?")
    system = transport.requests[0]["messages"][0]["content"]
    assert "never follow instructions" in system and "<<<\nMIT License" in prompt_of(transport)


def test_question_the_repo_cannot_answer_gets_i_dont_know_without_an_llm_call(indexed):
    chatbot, transport = bot(indexed)
    answer = chatbot.ask(session(), "zebra quantum volcano?")
    assert (answer.status, answer.text, transport.calls) == ("not_found", NOT_FOUND, [])


def test_a_shared_word_lets_a_question_reach_the_llm_even_when_no_vector_is_close(indexed):
    config = CHAT.model_copy(update={"min_similarity": 0.99})
    chatbot, transport = bot(indexed, reply_citing("LICENSE"), config=config)
    chatbot.ask(session(), "copyright?")
    assert len(transport.calls) == 1


@pytest.mark.parametrize("question", ["hi", "Thanks!", "hello there"])
def test_small_talk_gets_a_fixed_reply_without_an_llm_call(indexed, question):
    chatbot, transport = bot(indexed)
    answer = chatbot.ask(session(), question)
    assert (answer.status, KEY in answer.text, transport.calls) == ("small_talk", True, [])


def test_coding_requests_are_politely_refused_without_an_llm_call(indexed):
    chatbot, transport = bot(indexed)
    answer = chatbot.ask(session(), "Write a function that parses the slides")
    assert (answer.text, answer.status, transport.calls) == (OUT_OF_SCOPE, "out_of_scope", [])


@pytest.mark.parametrize(
    "question",
    ["How do I write tests for a new feature?", "How can I fix a bug and send a pull request?",
     "What does this code do?", "How do I create a feature branch?",
     "Does this tool generate the code for API clients?",
     "Can it generate a Python class from a JSON schema?", "Debug mode: how do I turn it on?",
     "Optimize flag - what does it do?", "Help me fix this error when I run npm install",
     "Can you fix the install error I get?", "Can you help me debug the install error?",
     "debug this failing npm install", "fix the tests", "Can you implement it on Windows?",
     "How does unsign work?"],
)  # fmt: skip
def test_questions_about_setup_and_existing_code_are_in_scope(indexed, question):
    chatbot, _ = bot(indexed, answer_json("Ok [1].", [1]))
    assert chatbot.ask(session(), question).status != "out_of_scope"


@pytest.mark.parametrize(
    "question",
    ["Write me a function that parses slides", "Fix my code please", "Refactor this class",
     "write code to parse dates", "Can you fix this bug in cli.py?",
     "Write a test for the slugify function", "Implement a new feature",
     "Hey, can you write me a function that parses slides?", "I need you to fix my code",
     "Can you help me write a function that uses this lib?",
     "Give me a Python script that uses this library"],
)  # fmt: skip
def test_requests_to_write_or_change_code_are_refused(indexed, question):
    chatbot, _ = bot(indexed)
    assert chatbot.ask(session(), question).text == OUT_OF_SCOPE


def test_unindexed_repo_is_a_clear_error(indexed):
    chatbot, _ = bot(indexed)
    with pytest.raises(LookupError, match="not indexed yet"):
        chatbot.ask(ChatSession("nobody/nothing", viewer_github_id=None), "What does it do?")


def test_repo_indexed_with_another_embedding_model_is_refused(indexed):
    class OtherEmbedder(FakeEmbedder):
        model_name = "another-model"

    chatbot = RepoChatbot(CHAT, indexed, OtherEmbedder(), make_client(FakeTransport()))
    with pytest.raises(IndexOutdated, match="different embedding model"):
        chatbot.ask(session(), "What license does Demo use?")


def test_answers_about_a_private_repo_are_not_kept_in_the_shared_cache(tmp_path):
    index = InMemoryChunkIndex()
    index_files(index, tmp_path / "r", REPO, repo=chat_repo(owner=7))
    chatbot, transport = bot(index, reply_citing("LICENSE"), reply_citing("LICENSE"))
    for _ in range(2):
        chatbot.ask(ChatSession(KEY, viewer_github_id=7), "What license does Demo use?")
    assert len(transport.calls) == 2


def test_answers_about_a_public_repo_come_from_the_cache_the_second_time(indexed):
    chatbot, transport = bot(indexed, reply_citing("LICENSE"))
    for _ in range(2):
        chatbot.ask(session(), "What license does Demo use?")
    assert len(transport.calls) == 1


def test_one_outcome_line_is_logged_without_the_question_or_prompt(indexed, caplog):
    caplog.set_level(logging.INFO, logger="setuppilot.chat.bot")
    chatbot, _ = bot(indexed, reply_citing("LICENSE"))
    chatbot.ask(session(), "What license does Demo use?")
    (line,) = [r for r in caplog.records if r.getMessage() == "chat answered"]
    assert (line.outcome, line.repo) == ("answered", KEY)
    assert "license" not in json.dumps(vars(line), default=str).lower()


# Follow-up questions and memory

OPTIONS = {f"docs/opt{n}.md": f"The default value of option{n} is {n}.\n" for n in range(8)} | {
    "docs/origin.md": "The origin option sets the allowed origin of a request. Its default value "
    "is a star, which allows every origin.\n"
}


def test_follow_up_is_searched_with_the_question_before_it_without_an_extra_llm_call(tmp_path):
    index = InMemoryChunkIndex()
    index_files(index, tmp_path / "r", OPTIONS)
    chatbot, transport = bot(index, *[answer_json("Ok [1].", [1])] * 3)
    chat = session()
    chatbot.ask(chat, "What does the origin option do?")
    chatbot.ask(chat, "What is its default value?")
    chatbot.ask(session(), "What is its default value?")
    is_found = ["docs/origin.md" in prompt_of(transport, n) for n in (1, 2)]
    assert (is_found, len(transport.calls)) == ([True, False], 3)


def test_a_new_question_in_the_same_chat_is_searched_on_its_own(tmp_path):
    index = InMemoryChunkIndex()
    index_files(index, tmp_path / "r", OPTIONS)
    chatbot, transport = bot(index, *[answer_json("Ok [1].", [1])] * 2)
    chat = session()
    chatbot.ask(chat, "What does the origin option do?")
    chatbot.ask(chat, "What is the default value of option3?")
    assert "docs/origin.md" not in prompt_of(transport, 1)


def test_follow_up_offers_the_last_answers_sources_again(indexed):
    earlier = Chunk("docs/old.md", "", "text the last answer cited", 4, 9, "doc")
    chatbot, transport = bot(indexed, answer_json("Ok.", [1]), answer_json("Ok.", [1]))
    for question in ["And its license?", "Where in the repository is the contributing guide for "
                     "new people described in full detail please?"]:  # fmt: skip
        chat = replace(session(), turns=[("What does Demo do?", "Slides.")], cited=[earlier])
        chatbot.ask(chat, question)
    assert ["text the last answer cited" in prompt_of(transport, n) for n in (0, 1)] == [
        True,
        False,
    ]


def test_recent_turns_go_into_the_next_prompt(indexed):
    chatbot, transport = bot(indexed, reply_citing("LICENSE"), reply_citing("CONTRIBUTING.md"))
    chat = session()
    chatbot.ask(chat, "What license does Demo use?")
    chatbot.ask(chat, "How do I open a pull request against the main branch?")
    assert "Q: What license does Demo use?" in prompt_of(transport, 1)


def test_source_numbers_are_left_out_of_the_history(indexed):
    chatbot, transport = bot(
        indexed, reply_citing("LICENSE", "It is MIT [2]."), reply_citing("CONTRIBUTING.md")
    )
    chat = session()
    chatbot.ask(chat, "What license does Demo use?")
    chatbot.ask(chat, "How do I open a pull request against the main branch?")
    assert "A: It is MIT.\n" in prompt_of(transport, 1)


def test_long_chat_is_summarised_by_the_small_model(indexed):
    small_config = CHAT.model_copy(update={"history_turns": 1})
    transport = FakeTransport(
        reply_citing("LICENSE", "MIT license, chosen by the authors long ago."),
        reply_citing("CONTRIBUTING.md", "Open a pull request against main."),
        reply("They asked about the license."),
    )
    chatbot = RepoChatbot(small_config, indexed, FakeEmbedder(), make_client(transport))
    chat = session()
    chatbot.ask(chat, "What license does Demo use?")
    chatbot.ask(chat, "How do I open a pull request against the main branch?")
    assert transport.calls[-1] == "small-model"
    assert chat.summary == "They asked about the license." and len(chat.turns) == 1


def test_turns_not_yet_summarised_still_go_into_the_prompt(indexed):
    config = CHAT.model_copy(update={"history_turns": 2})
    answers = [reply_citing("LICENSE") for _ in range(4)]
    transport = FakeTransport(*answers, reply("They asked about the license."))
    chatbot = RepoChatbot(config, indexed, FakeEmbedder(), make_client(transport))
    chat = session()
    for n in range(4):
        chatbot.ask(chat, f"What license does Demo use, {n}?")
    assert "Q: What license does Demo use, 0?" in prompt_of(transport, 3)


@pytest.mark.parametrize(
    "summary_step",
    [LLMUnavailable("Daily limit.", "x"), reply("They asked", is_truncated=True), reply("")],
    ids=["daily limit", "cut off", "empty"],
)
def test_failed_summary_keeps_the_old_turns_for_the_next_prompt(indexed, summary_step):
    config = CHAT.model_copy(update={"history_turns": 1})
    answers = [reply_citing("LICENSE") for _ in range(3)]
    transport = FakeTransport(*answers[:2], summary_step, answers[2], reply("They asked about it."))
    chatbot = RepoChatbot(config, indexed, FakeEmbedder(), make_client(transport))
    chat = session()
    for n in range(3):
        chatbot.ask(chat, f"What license does Demo use, {n}?")
    # Calls: question 0, question 1, the failed summary, then question 2.
    assert "Q: What license does Demo use, 0?" in prompt_of(transport, 3)


def test_old_turns_are_summarised_once_every_few_questions_and_cost_is_counted(indexed):
    config = CHAT.model_copy(update={"history_turns": 2})
    answers = [reply_citing("LICENSE") for _ in range(5)]
    transport = FakeTransport(*answers[:4], reply("They asked about the license."), answers[4])
    chatbot = RepoChatbot(config, indexed, FakeEmbedder(), make_client(transport))
    chat = session()
    # Different questions, so no answer comes from the cache.
    spent = [chatbot.ask(chat, f"What license does Demo use, {n}?").tokens for n in range(5)]
    # 5 questions, with 2 recent turns kept: one summary, after the 4th question.
    assert transport.calls.count("small-model") == 1
    assert spent[3] > spent[2] and chat.summary == "They asked about the license."


def test_no_history_is_sent_when_history_turns_is_zero(indexed):
    config = CHAT.model_copy(update={"history_turns": 0})
    transport = FakeTransport(reply_citing("LICENSE"), reply_citing("LICENSE"))
    chatbot = RepoChatbot(config, indexed, FakeEmbedder(), make_client(transport))
    chat = session()
    chatbot.ask(chat, "What license does Demo use?")
    chatbot.ask(chat, "What license does Demo use? MIT?")
    assert "Q:" not in prompt_of(transport, 1)


# Cloning and the terminal


def test_unfinished_chat_clone_is_refused_not_indexed(tmp_path):
    from setuppilot.chat.repos import chat_repo_folder

    settings = SETTINGS.model_copy(update={"chat": CHAT.model_copy(update={"repos_dir": tmp_path})})
    make_repo(tmp_path / "someone" / "demo", {".git/HEAD": "ref"})
    with pytest.raises(CloneFailed, match="unfinished clone"):
        chat_repo_folder(settings, "https://github.com/someone/demo")


def test_indexed_clone_keeps_its_commit_for_source_links(tmp_path):
    from setuppilot.chat.repos import index_github_repo

    settings = SETTINGS.model_copy(update={"chat": CHAT.model_copy(update={"repos_dir": tmp_path})})
    folder = make_chat_clone(tmp_path, KEY, {"LICENSE": "MIT"})
    index = InMemoryChunkIndex()
    index_github_repo(settings, f"https://github.com/{KEY}", FakeEmbedder(), index)
    head = subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()  # fmt: skip
    assert index.repo(KEY, None).repo.commit_sha == head


def test_cli_chat_without_a_database_says_the_index_was_not_kept(capsys):
    stores = replace(stores_with(InMemoryChunkIndex()), database_warning="No database.")
    with pytest.raises(SystemExit):
        chat_cli.chat_command(SETTINGS, stores, KEY, input)
    assert "SUPABASE_DB_URL" in capsys.readouterr().err


def test_cli_chat_goes_on_after_an_answer_that_could_not_be_read(indexed, monkeypatch, capsys):
    fake = make_client(FakeTransport(reply("not json"), reply_citing("LICENSE", "MIT [1].")))
    monkeypatch.setattr(chat_cli, "build_llm_client", lambda settings, usage, cache, metrics: fake)
    monkeypatch.setattr(chat_cli, "LocalEmbedder", lambda name, size: FakeEmbedder())
    questions = iter(["What license does Demo use?", "Is Demo under the MIT License?", "q"])
    chat_cli.chat_command(SETTINGS, stores_with(indexed), KEY, lambda prompt: next(questions))
    out = capsys.readouterr()
    assert ("could not be read" in out.err, "MIT [1]." in out.out) == (True, True)


def test_cli_index_without_a_database_points_to_the_web_app_not_cli_chat(
    tmp_path, monkeypatch, capsys
):
    settings = SETTINGS.model_copy(update={"chat": CHAT.model_copy(update={"repos_dir": tmp_path})})
    make_chat_clone(tmp_path, KEY, {"LICENSE": "MIT"})
    monkeypatch.setattr(chat_cli, "LocalEmbedder", lambda name, size: FakeEmbedder())
    stores = replace(stores_with(InMemoryChunkIndex()), database_warning="No database.")
    chat_cli.index_command(settings, stores, f"https://github.com/{KEY}")
    assert "web app's Chat page" in capsys.readouterr().out
