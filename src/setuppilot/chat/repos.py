"""Clone a repo for the chatbot once, and refuse a clone that never finished."""

from pathlib import Path

from setuppilot.chat.bot import IndexResult, index_repo
from setuppilot.chat.embed import Embedder
from setuppilot.chat.index import ChunkIndex
from setuppilot.config import Settings
from setuppilot.errors import CloneFailed
from setuppilot.tools.clone import check_github_repo, clone_repo
from setuppilot.tools.github_url import parse_github_url

# Written only after a clone fully works, so a refused or cut-off clone is never indexed.
_DONE_MARKER = ".git/setuppilot-clone-done"


def chat_repo_folder(settings: Settings, url: str) -> tuple[str, str, Path]:
    """The repo key ("owner/name"), its clean URL and its folder, cloning it if needed."""
    ref = parse_github_url(url)
    owner_dir = settings.chat.repos_dir / ref.owner
    folder = owner_dir / ref.name
    if folder.exists() and not (folder / _DONE_MARKER).exists():
        raise CloneFailed(
            "An unfinished clone of this repo is in the way.",
            details=f"Delete {folder} yourself if you do not need it, then try again.",
        )
    if not folder.exists():
        owner_dir.mkdir(parents=True, exist_ok=True)
        clone_repo(ref, owner_dir, settings.repo, check_github_repo)
        (folder / _DONE_MARKER).touch()
    return repo_key(url), ref.url, folder


def repo_key(url: str) -> str:
    """The name the chat index keeps a repo under."""
    ref = parse_github_url(url)
    return f"{ref.owner}/{ref.name}"


def index_github_repo(
    settings: Settings, url: str, embedder: Embedder, index: ChunkIndex
) -> tuple[str, IndexResult]:
    """Clone (if needed) and index one repo for the chatbot. Returns its key and the result."""
    key, clean_url, folder = chat_repo_folder(settings, url)
    result = index_repo(
        key, clean_url, folder, settings.chat, settings.repo.max_tree_chars, embedder, index
    )
    return key, result
