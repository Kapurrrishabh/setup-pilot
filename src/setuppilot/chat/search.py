"""Word search next to vector search: the words of a text, BM25 for the in-memory index, and
reciprocal rank fusion to merge rankings. The Postgres index searches the same words."""

import math
import re
from collections import Counter
from collections.abc import Hashable, Sequence

_STOP_WORDS = frozenset(
    {"a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "in", "on", "for", "and",
     "or", "it", "its", "this", "that", "these", "those", "do", "does", "did", "how", "what",
     "which", "who", "whom", "why", "when", "where", "can", "could", "i", "you", "my", "me",
     "with", "by", "from", "as", "at", "use", "used", "using", "get"}
)  # fmt: skip
_ENDINGS = ("ing", "ed", "es", "s")
_WORD = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)*")
_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
# The usual constant for reciprocal rank fusion (Cormack et al., 2009; Supabase's hybrid search).
_RRF_K = 60
# BM25's usual settings.
_K1, _B = 1.2, 0.75


def tokens(text: str) -> list[str]:
    """Lowercase words: camelCase and snake_case split, common words dropped, plain endings cut,
    version numbers kept whole. So "isOriginAllowed" matches "origin allowed", and "3.10" is one
    word."""
    words = []
    for word in _WORD.findall(_CAMEL.sub(r"\1 \2", text)):
        word = word.lower()
        if word in _STOP_WORDS or len(word) < 2:
            continue
        for ending in _ENDINGS:
            if len(word) > len(ending) + 2 and word.endswith(ending):
                word = word[: -len(ending)]
                break
        words.append(word)
    return words


def fuse_rankings[T: Hashable](rankings: Sequence[Sequence[T]]) -> list[T]:
    """Items from several best-first rankings, ordered by reciprocal rank fusion. Ties keep the
    order of the first ranking."""
    scores: dict[T, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1 / (_RRF_K + rank)
    return sorted(scores, key=lambda item: -scores[item])


class WordIndex:
    """BM25 scores of word lists for a fixed set of texts."""

    def __init__(self, texts: list[str]) -> None:
        self._counts = [Counter(tokens(text)) for text in texts]
        self._lengths = [sum(c.values()) for c in self._counts]
        self._average = sum(self._lengths) / max(len(self._lengths), 1)
        having = Counter(word for counts in self._counts for word in counts)
        n = len(texts)
        self._idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in having.items()}

    def scores(self, terms: list[str]) -> list[float]:
        """One score per text; 0 when it has none of the terms."""
        scores = []
        for counts, length in zip(self._counts, self._lengths, strict=True):
            score = 0.0
            for term in terms:
                if found := counts.get(term):
                    norm = _K1 * (1 - _B + _B * length / self._average)
                    score += self._idf[term] * found * (_K1 + 1) / (found + norm)
            scores.append(score)
        return scores
