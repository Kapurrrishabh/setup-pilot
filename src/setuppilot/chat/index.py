"""Where chunks and their vectors live: pgvector in Supabase, or memory for tests and fallback."""

from dataclasses import dataclass
from typing import Protocol

from setuppilot.chat.chunks import Chunk
from setuppilot.chat.search import WordIndex
from setuppilot.errors import NotFound


@dataclass(frozen=True)
class ChatRepo:
    """A repo the chatbot reads: its key ("owner/name"), URL, the commit read, and who may
    see it."""

    key: str
    url: str
    commit_sha: str
    # The GitHub user who may see this private repo; None for a public repo, which anyone may see.
    owner_github_id: int | None

    @property
    def is_private(self) -> bool:
        return self.owner_github_id is not None


@dataclass(frozen=True)
class IndexedRepo:
    repo: ChatRepo
    # Vectors from another model cannot be compared with this one's, so it is kept per repo.
    embedding_model: str
    card: tuple[Chunk, ...]


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


@dataclass(frozen=True)
class SearchResults:
    # Best first: cosine similarity for vectors, a word-match score for words. Only chunks that
    # share a word with the question are in by_words.
    by_vector: list[Hit]
    by_words: list[Hit]


class ChunkIndex(Protocol):
    """viewer_github_id is the signed-in GitHub user asking, or None when nobody is signed in.
    A private repo someone else owns is reported as not indexed, so its name does not leak."""

    def replace_repo(
        self, indexed: IndexedRepo, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None: ...
    def repo(self, repo_key: str, viewer_github_id: int | None) -> IndexedRepo: ...
    def repos(self, viewer_github_id: int | None) -> list[str]: ...
    def search(
        self,
        repo_key: str,
        viewer_github_id: int | None,
        vector: list[float],
        terms: list[str],
        limit: int,
    ) -> SearchResults: ...


def can_see(repo: ChatRepo, viewer_github_id: int | None) -> bool:
    return repo.owner_github_id is None or repo.owner_github_id == viewer_github_id


@dataclass(frozen=True)
class _StoredRepo:
    indexed: IndexedRepo
    rows: list[tuple[Chunk, list[float]]]
    words: WordIndex


class InMemoryChunkIndex:
    def __init__(self) -> None:
        self._repos: dict[str, _StoredRepo] = {}

    def replace_repo(
        self, indexed: IndexedRepo, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None:
        rows = list(zip(chunks, vectors, strict=True))
        words = WordIndex([c.search_text for c in chunks])
        self._repos[indexed.repo.key] = _StoredRepo(indexed, rows, words)

    def repo(self, repo_key: str, viewer_github_id: int | None) -> IndexedRepo:
        return self._stored(repo_key, viewer_github_id).indexed

    def repos(self, viewer_github_id: int | None) -> list[str]:
        visible = (k for k, s in self._repos.items() if can_see(s.indexed.repo, viewer_github_id))
        return sorted(visible)

    def search(
        self,
        repo_key: str,
        viewer_github_id: int | None,
        vector: list[float],
        terms: list[str],
        limit: int,
    ) -> SearchResults:
        stored = self._stored(repo_key, viewer_github_id)
        by_vector = [
            Hit(chunk, sum(a * b for a, b in zip(vector, saved, strict=True)))
            for chunk, saved in stored.rows
        ]
        scores = stored.words.scores(terms)
        by_words = [Hit(c, s) for (c, _), s in zip(stored.rows, scores, strict=True) if s > 0]
        return SearchResults(_best(by_vector, limit), _best(by_words, limit))

    def _stored(self, repo_key: str, viewer_github_id: int | None) -> _StoredRepo:
        stored = self._repos.get(repo_key)
        if stored is None or not can_see(stored.indexed.repo, viewer_github_id):
            raise unknown_repo(repo_key)
        return stored


def _best(hits: list[Hit], limit: int) -> list[Hit]:
    return sorted(hits, key=lambda h: h.score, reverse=True)[:limit]


def unknown_repo(repo_key: str) -> NotFound:
    return NotFound(f"{repo_key!r} is not indexed yet. Run: setuppilot index <its GitHub URL>")
