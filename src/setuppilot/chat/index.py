"""Where chunks and their vectors live: pgvector in Supabase, or memory for tests and fallback."""

from dataclasses import dataclass
from typing import Protocol

from setuppilot.chat.chunks import Chunk
from setuppilot.errors import NotFound


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    similarity: float


class ChunkIndex(Protocol):
    def replace_repo(
        self, repo_key: str, url: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None: ...
    def search(self, repo_key: str, vector: list[float], limit: int) -> list[Hit]: ...
    def repos(self) -> list[str]: ...


class InMemoryChunkIndex:
    def __init__(self) -> None:
        self._repos: dict[str, list[tuple[Chunk, list[float]]]] = {}

    def replace_repo(
        self, repo_key: str, url: str, chunks: list[Chunk], vectors: list[list[float]]
    ) -> None:
        self._repos[repo_key] = list(zip(chunks, vectors, strict=True))

    def search(self, repo_key: str, vector: list[float], limit: int) -> list[Hit]:
        if repo_key not in self._repos:
            raise unknown_repo(repo_key)
        hits = [
            Hit(chunk, sum(a * b for a, b in zip(vector, stored, strict=True)))
            for chunk, stored in self._repos[repo_key]
        ]
        return sorted(hits, key=lambda h: h.similarity, reverse=True)[:limit]

    def repos(self) -> list[str]:
        return sorted(self._repos)


def unknown_repo(repo_key: str) -> NotFound:
    return NotFound(f"{repo_key!r} is not indexed yet. Run: setuppilot index <its GitHub URL>")
