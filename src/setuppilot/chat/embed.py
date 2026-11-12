"""Turn text into vectors on this machine, with a small sentence-transformers model."""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

# Embedding a full index (1,500 chunks) peaked at 2.0 GB in batches of 32, the library's default,
# and at 1.1 GB in batches of 8, which took 0.6 s longer (measured 2026-11-12).
_BATCH_SIZE = 8


class Embedder(Protocol):
    model_name: str
    dimensions: int

    @property
    def max_tokens(self) -> int:
        """How many tokens of text the model reads; it silently drops the rest. Chunks are cut
        to fit, so this is the one place the window is set."""

    def count_tokens(self, text: str) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One unit-length vector per text, so a dot product is the cosine similarity."""


class LocalEmbedder:
    def __init__(self, model_name: str, dimensions: int) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self._model: SentenceTransformer | None = None

    @property
    def max_tokens(self) -> int:
        model = self._loaded()
        # max_seq_length counts the model's own start and end tokens too.
        return model.max_seq_length - model.tokenizer.num_special_tokens_to_add()

    def count_tokens(self, text: str) -> int:
        return len(self._loaded().tokenizer(text, add_special_tokens=False)["input_ids"])

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._loaded().encode(
            texts, batch_size=_BATCH_SIZE, normalize_embeddings=True, show_progress_bar=False
        )
        return [v.tolist() for v in vectors]

    def _loaded(self) -> "SentenceTransformer":
        if self._model is None:
            # Imported late: loading torch takes seconds, and only the chatbot needs it.
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self.model_name, device="cpu")
            size = model.get_sentence_embedding_dimension()
            if size != self.dimensions:
                raise ValueError(
                    f"{self.model_name} makes vectors of size {size}, but "
                    f"chat.embedding_dimensions is {self.dimensions}. Change both together."
                )
            self._model = model
        return self._model
