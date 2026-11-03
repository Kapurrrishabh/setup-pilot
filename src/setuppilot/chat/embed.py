"""Turn text into vectors on this machine, with a small sentence-transformers model."""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One unit-length vector per text, so a dot product is the cosine similarity."""


class LocalEmbedder:
    def __init__(self, model_name: str, dimensions: int) -> None:
        self.dimensions = dimensions
        self._model_name = model_name
        self._model: SentenceTransformer | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            # Imported late: loading torch takes seconds, and only the chatbot needs it.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name, device="cpu")
            size = self._model.get_sentence_embedding_dimension()
            if size != self.dimensions:
                raise ValueError(
                    f"{self._model_name} makes vectors of size {size}, but "
                    f"chat.embedding_dimensions is {self.dimensions}. Change both together."
                )
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]
