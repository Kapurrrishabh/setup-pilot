"""The chatbot reads a repo straight from GitHub when there is no clone, and keeps a private repo
for the signed-in user only."""

import httpx
import pytest

from setuppilot.chat import repos
from setuppilot.chat.bot import index_repo
from setuppilot.chat.index import ChatRepo, InMemoryChunkIndex
from setuppilot.chat.repos import index_for_chat
from setuppilot.chat.search import tokens
from setuppilot.config import load_settings
from setuppilot.errors import CloneFailed
from setuppilot.tools.clone import CloneRequest, GitHubAccess
from tests.fakes import FakeClock, FakeEmbedder, make_chat_clone, make_repo
from tests.github_fakes import (
    COMMIT,
    REPO_URL,
    USER,
    FakeGitHub,
    MemoryKeyring,
    client_for,
    git_repo,
    github_access,
    serve_repo,
    targz,
    tree_json,
)
from tests.test_config import REAL_CONFIG

URL = "https://github.com/o/r"
KEY = "o/r"
FILES = {
    "README.md": "# Demo\n\nInstall it with pip install demo.\n",
    "LICENSE": "MIT License\n",
    "pyproject.toml": "[project]\nname = 'demo'\nlicense = 'MIT'\n",
    "src/demo/cli.py": "def main():\n    return 'demo'\n",
    "node_modules/left/README.md": "never read\n",
    "logo.png": "\x00PNG",
}
FILE_BYTES = {path: text.encode() for path, text in FILES.items()}


@pytest.fixture
def settings(tmp_path):
    settings = load_settings(REAL_CONFIG)
    chat = settings.chat.model_copy(update={"repos_dir": tmp_path / "chat-repos"})
    return settings.model_copy(update={"chat": chat})


@pytest.fixture
def cloned(monkeypatch):
    """Clones for the chatbot made from FILES, without the network. Records each clone's GitHub
    access and request."""
    made = []

    def clone(ref, dest_dir, config, github, request):
        made.append((github, request))
        return git_repo(dest_dir / ref.name, FILES)

    monkeypatch.setattr(repos, "clone_repo", clone)
    return made


def public(fake):
    return GitHubAccess(client_for(fake), signed_in=None)


def signed_in(fake):
    return github_access(fake, FakeClock(), MemoryKeyring(), "ghu_chatToken")


def every_chunk(index):
    (vector,) = FakeEmbedder().embed(["demo"])
    found = index.search(KEY, USER.id, vector, tokens("demo"), 1000).by_vector
    return sorted((h.chunk for h in found), key=lambda c: (c.path, c.start_line))


def test_a_repo_without_a_clone_is_read_from_github_into_the_same_index_as_its_clone(
    tmp_path, settings
):
    fake = FakeGitHub()
    serve_repo(fake, targz(FILE_BYTES), tree_json(FILE_BYTES))
    from_github = InMemoryChunkIndex()
    key, result = index_for_chat(settings, URL, public(fake), "public", FakeEmbedder(), from_github)
    from_clone = InMemoryChunkIndex()
    repo = ChatRepo(KEY, URL, COMMIT, owner_github_id=None)
    folder = make_repo(tmp_path / "clone", FILES)
    index_repo(
        repo, folder, settings.chat, settings.repo.max_tree_chars, FakeEmbedder(), from_clone
    )
    assert key == KEY and from_github.repo(KEY, None) == from_clone.repo(KEY, None)
    assert every_chunk(from_github) == every_chunk(from_clone) and result.chunks > 0
    assert not settings.chat.repos_dir.exists()


def test_a_private_repo_read_through_the_sign_in_is_kept_for_its_owner_only(settings):
    fake = FakeGitHub()
    serve_repo(fake, targz(FILE_BYTES), tree_json(FILE_BYTES), is_private=True)
    index = InMemoryChunkIndex()
    index_for_chat(settings, URL, signed_in(fake), "github", FakeEmbedder(), index)
    assert (index.repos(None), index.repos(USER.id)) == ([], [KEY])
    assert index.repo(KEY, USER.id).repo.owner_github_id == USER.id


def test_a_public_repo_read_through_the_sign_in_is_open_to_everyone(settings):
    fake = FakeGitHub()
    serve_repo(fake, targz(FILE_BYTES), tree_json(FILE_BYTES), is_private=False)
    index = InMemoryChunkIndex()
    index_for_chat(settings, URL, signed_in(fake), "github", FakeEmbedder(), index)
    assert index.repos(None) == [KEY]


def test_a_private_or_missing_repo_without_sign_in_says_to_sign_in(settings, cloned):
    fake = FakeGitHub()
    fake.on("GET", REPO_URL, httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(CloneFailed, match="private or missing"):
        index_for_chat(settings, URL, public(fake), "public", FakeEmbedder(), InMemoryChunkIndex())
    assert cloned == []


def too_big(settings):
    github = settings.github.model_copy(update={"max_tarball_mb": 1})
    return settings.model_copy(update={"github": github}), {"big.bin": b"x" * (2**20 + 1)}


def test_a_public_repo_too_big_to_read_is_cloned_instead(settings, cloned):
    settings, files = too_big(settings)
    fake = FakeGitHub()
    serve_repo(fake, targz(files), tree_json(files))
    github, index = public(fake), InMemoryChunkIndex()
    index_for_chat(settings, URL, github, "public", FakeEmbedder(), index)
    # The app's own GitHub access, not a new one per clone.
    ((used, request),) = cloned
    assert (used is github, request) == (True, CloneRequest("public", None, None))
    assert index.repos(None) == [KEY]


def test_a_private_repo_too_big_to_read_is_refused_and_never_cloned(settings, cloned):
    settings, files = too_big(settings)
    fake = FakeGitHub()
    serve_repo(fake, targz(files), tree_json(files), is_private=True)
    with pytest.raises(CloneFailed) as refused:
        index_for_chat(
            settings, URL, signed_in(fake), "github", FakeEmbedder(), InMemoryChunkIndex()
        )
    details = refused.value.details
    assert "never cloned for chat" in details and "Clone it" not in details
    assert cloned == []


def test_a_public_repo_github_cannot_read_now_is_cloned_instead(settings, cloned):
    fake = FakeGitHub()
    used_up = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800000000"}
    fake.on("GET", REPO_URL, httpx.Response(403, headers=used_up))
    index = InMemoryChunkIndex()
    index_for_chat(settings, URL, public(fake), "public", FakeEmbedder(), index)
    assert len(cloned) == 1 and index.repos(None) == [KEY]


def test_an_existing_clone_is_indexed_without_asking_github(settings):
    make_chat_clone(settings.chat.repos_dir, KEY, FILES)
    fake, index = FakeGitHub(), InMemoryChunkIndex()
    index_for_chat(settings, URL, public(fake), "public", FakeEmbedder(), index)
    assert fake.requests == [] and index.repos(None) == [KEY]
