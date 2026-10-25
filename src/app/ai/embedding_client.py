"""
Booklet_AI Desktop — OpenRouter Embedding Client (STEP-3.1)

Provides embedding generation via OpenRouter's OpenAI-compatible embeddings
endpoint. Used by QuestionIndexer to populate vector embeddings for
ministry_question blocks in the local SQLite question bank.

Follows the same pattern as OpenRouterClient in STEP-1.4:
- API key retrieved securely at runtime (never logged).
- Configurable model (defaults to text-embedding-3-small-class, 1536 dims).
- Clean exception propagation allowing graceful degradation in QuestionIndexer.
"""

import logging
from typing import List, Optional, Protocol, Union

import httpx

from app.core.exceptions import BookletError

logger = logging.getLogger("booklet_ai.ai.embedding")

_DEFAULT_EMBEDDING_MODEL = "openai/text-embedding-3-small"
_DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/embeddings"
_DEFAULT_TIMEOUT_SEC = 15.0


class EmbeddingError(BookletError):
    """Raised when an embedding request to the remote API fails."""


class EmbeddingClientProtocol(Protocol):
    """Protocol defining the embedding client interface."""

    def embed(self, text: str) -> List[float]:
        """Generate a 1D vector embedding for the input text."""
        ...

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate vector embeddings for a batch of text inputs."""
        ...


class OpenRouterEmbeddingClient:
    """OpenRouter-hosted embedding endpoint client.

    Attributes:
        api_key: OpenRouter API key (retrieved via SecretsManager or caller).
        model: Embedding model identifier (default: openai/text-embedding-3-small).
        endpoint: API endpoint URL.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        model: str = _DEFAULT_EMBEDDING_MODEL,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout

    def embed(self, text: str) -> List[float]:
        """Generate an embedding vector for a single text string.

        Args:
            text: Text to embed. Must not be empty.

        Returns:
            List of floats representing the embedding vector.

        Raises:
            EmbeddingError: If API key is missing, network fails, or response is invalid.
        """
        if not text or not text.strip():
            raise EmbeddingError("Cannot generate embedding for empty text.")

        batch_result = self.embed_batch([text])
        if not batch_result:
            raise EmbeddingError("Empty response returned from embedding endpoint.")
        return batch_result[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embedding vectors for multiple text strings in a single call.

        Args:
            texts: List of non-empty text strings to embed.

        Returns:
            List of embedding vectors corresponding to the inputs in order.

        Raises:
            EmbeddingError: If API key is missing, network fails, or response is invalid.
        """
        if not self.api_key:
            raise EmbeddingError("OpenRouter API key is not configured for embeddings.")

        cleaned_texts = [t.strip() for t in texts if t and t.strip()]
        if not cleaned_texts:
            return []

        payload = {
            "model": self.model,
            "input": cleaned_texts if len(cleaned_texts) > 1 else cleaned_texts[0],
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()

            if "data" not in data or not isinstance(data["data"], list):
                raise EmbeddingError(f"Malformed embedding API response: {data}")

            # Sort items by 'index' to guarantee alignment with inputs
            items = sorted(data["data"], key=lambda item: item.get("index", 0))
            vectors = [item["embedding"] for item in items if "embedding" in item]

            if len(vectors) != len(cleaned_texts):
                raise EmbeddingError(
                    f"Expected {len(cleaned_texts)} embeddings, but received {len(vectors)}."
                )

            return vectors

        except httpx.HTTPError as exc:
            err_msg = f"HTTP error during embedding generation: {exc}"
            logger.warning(err_msg)
            raise EmbeddingError(err_msg) from exc
        except Exception as exc:
            err_msg = f"Unexpected error during embedding generation: {exc}"
            logger.warning(err_msg)
            raise EmbeddingError(err_msg) from exc
