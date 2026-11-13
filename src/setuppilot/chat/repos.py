"""Read a repo for the chatbot straight from GitHub, or from a clone made once; a clone that never
finished is refused."""

import logging
from pathlib import Path

from setuppilot.chat.bot import IndexResult, index_documents, index_repo
from setuppilot.chat.collect import documents_from_snapshot, file_kind
from setuppilot.chat.embed import Embedder
from setuppilot.chat.index import ChatRepo, ChunkIndex
from setuppilot.config import Settings
from setuppilot.errors import CloneFailed
from setuppilot.github.api import GitHubAuthEnded, GitHubError, GitHubNotFound
from setuppilot.github.reader import RepoSnapshot, SnapshotTooBig, read_snapshot
from setuppilot.tools.clone import (
    CloneRequest,
    GitHubAccess,
    RepoAccess,
    SignedIn,
    clone_repo,
    head_commit,
    not_found,
    public_github,
    reader_for,
)
from setuppilot.tools.github_url import parse_github_url

log = logging.getLogger(__name__)

# Written only after a clone fully works, so a refused or cut-off clone is never indexed.
_DONE_MARKER = ".git/setuppilot-clone-done"


def chat_repo_folder(settings: Settings, url: str, github: GitHubAccess) -> tuple[str, str, Path]:
    """The repo key ("owner/name"), its clean URL and its folder, cloning it if needed. Only
    public repos are cloned, because an index made from a clone is open to every viewer."""
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
        clone_repo(ref, owner_dir, settings.repo, github, CloneRequest("public", None, None))
        (folder / _DONE_MARKER).touch()
    return repo_key(url), ref.url, folder


def repo_key(url: str) -> str:
    """The name the chat index keeps a repo under."""
    ref = parse_github_url(url)
    return f"{ref.owner}/{ref.name}"


def index_github_repo(
    settings: Settings, url: str, embedder: Embedder, index: ChunkIndex
) -> tuple[str, IndexResult]:
    """Index one public repo for the chatbot, without sign-in, for the terminal chat.
    Returns its key and the result."""
    return index_for_chat(settings, url, public_github(), "public", embedder, index)


def index_for_chat(
    settings: Settings, url: str, github: GitHubAccess, access: RepoAccess, embedder: Embedder,
    index: ChunkIndex,
) -> tuple[str, IndexResult]:  # fmt: skip
    """Index a repo from its clone when there is one, otherwise from GitHub without a clone. A
    private repo is kept for the signed-in user only."""
    ref = parse_github_url(url)
    if not (settings.chat.repos_dir / ref.owner / ref.name).exists():
        client, signed_in = reader_for(github, access)
        config = settings.chat
        try:
            snapshot = read_snapshot(
                ref, client, settings.github, _is_chat_file, config.max_file_bytes
            )
        except GitHubNotFound as error:
            raise not_found(ref, access) from error
        except SnapshotTooBig as error:
            # Too big to read without a clone; but only a public repo may be cloned.
            if access == "github" and client.get_repo(ref).is_private:
                raise CloneFailed(
                    error.message,
                    details="A private repo is read from GitHub only, never cloned for chat, "
                    "because a clone's index is open to every viewer. Raise "
                    "github.max_tarball_mb in config.yaml to read it.",
                ) from error
        except GitHubError as error:
            if access == "github":
                raise
            # git is not held to the API's read limit, so a public repo can still be cloned.
            log.warning("github read skipped", extra={"reason": error.message, "repo": ref.url})
        else:
            repo = ChatRepo(
                f"{snapshot.ref.owner}/{snapshot.ref.name}",
                snapshot.ref.url,
                snapshot.commit_sha,
                _owner(snapshot, signed_in),
            )
            documents = documents_from_snapshot(
                snapshot.entries, snapshot.files, settings.repo.max_tree_chars
            )
            return repo.key, index_documents(repo, documents, config, embedder, index)
    key, clean_url, folder = chat_repo_folder(settings, url, github)
    repo = ChatRepo(key, clean_url, head_commit(folder, settings.repo), owner_github_id=None)
    result = index_repo(repo, folder, settings.chat, settings.repo.max_tree_chars, embedder, index)
    return key, result


def _is_chat_file(path: str) -> bool:
    return file_kind(path) is not None


def _owner(snapshot: RepoSnapshot, signed_in: SignedIn | None) -> int | None:
    """Who may see the indexed text: a private repo is never saved without its owner."""
    if not snapshot.is_private:
        return None
    user = None if signed_in is None else signed_in.vault.user
    if user is None:
        raise GitHubAuthEnded(
            "You are not signed in to GitHub any more, so this private repo was not indexed.",
            details="Sign in with GitHub, then index it again.",
        )
    return user.id
