"""Embedding provider interfaces, Ollama embedding client, and test fakes for ORBIT."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
import hashlib
import math
import os
import re
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from core.offline import is_loopback

# Global telemetry tracking for diagnostics
CPU_OPTIONS_REJECTED: bool = False
LAST_EMBED_FALLBACK_REASON: str = ""


class EmbeddingError(Exception):
    """Raised when text embedding fails due to connection, model, timeout, or validation errors."""
    pass


def dot_product(v1: list[float], v2: list[float]) -> float:
    """Compute cosine dot product between two unit-normalized float vectors.

    Uses an import-guarded numpy fast path if numpy is available, falling back
    to pure-Python multiplication.
    """
    if not v1 or not v2:
        return 0.0
    try:
        import numpy as np
        return float(np.dot(np.asarray(v1, dtype=np.float32), np.asarray(v2, dtype=np.float32)))
    except Exception:
        return sum(x * y for x, y in zip(v1, v2))


def _l2_normalize(vec: list[float]) -> list[float]:
    """Verify finite values and defensively normalize a float vector to unit length."""
    for x in vec:
        if not math.isfinite(x):
            raise EmbeddingError("Embedding vector contains non-finite values (NaN or Inf)")
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        # Degenerate zero vector
        return [0.0] * len(vec)
    return [x / norm for x in vec]


class Embedder(ABC):
    """Abstract base class for document and query text embedders."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of text strings into unit-length float vectors."""
        pass


class OllamaEmbedder(Embedder):
    """Local embedding generator communicating with Ollama's /api/embed endpoint."""

    def __init__(
        self,
        model: Optional[str] = None,
        host: Optional[str] = None,
        timeout: float = 5.0,
        batch_size: int = 16,
        keep_alive: Optional[str] = None,
        cpu_only: Optional[bool] = None,
    ):
        from config import settings

        self.model = model or getattr(settings, "RAG_EMBED_MODEL", "embeddinggemma")
        self.host = (host or getattr(settings, "OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.timeout = float(timeout)
        self.batch_size = max(1, int(batch_size))
        self.keep_alive = keep_alive
        self.cpu_only = cpu_only if cpu_only is not None else getattr(settings, "RAG_EMBED_CPU_ONLY", True)

        self._validate_host_loopback()

    def _validate_host_loopback(self) -> None:
        """Enforce that the embedding host is loopback unless OLLAMA_ALLOW_REMOTE is enabled."""
        allow_remote = os.getenv("OLLAMA_ALLOW_REMOTE", "").strip().lower() in ("1", "true", "yes")
        if allow_remote:
            return

        parsed = urlparse(self.host if "://" in self.host else f"http://{self.host}")
        hostname = parsed.hostname or "localhost"
        if not is_loopback(hostname):
            raise EmbeddingError(
                f"Refusing non-loopback Ollama host '{self.host}'. Set OLLAMA_ALLOW_REMOTE=1 to allow."
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of text inputs in batches using Ollama POST /api/embed."""
        if not texts:
            return []

        all_vectors: list[list[float]] = []
        expected_dim: Optional[int] = None

        for start_idx in range(0, len(texts), self.batch_size):
            batch = texts[start_idx : start_idx + self.batch_size]
            batch_vectors = self._embed_batch(batch)

            for vec in batch_vectors:
                normed = _l2_normalize(vec)
                if expected_dim is None:
                    expected_dim = len(normed)
                elif len(normed) != expected_dim:
                    raise EmbeddingError(
                        f"Inconsistent vector dimensions returned: expected {expected_dim}, got {len(normed)}"
                    )
                all_vectors.append(normed)

        return all_vectors

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        """Send a single batch to Ollama with retry on rejected CPU options."""
        global CPU_OPTIONS_REJECTED

        url = f"{self.host}/api/embed"
        payload: dict[str, Any] = {
            "model": self.model,
            "input": batch,
        }
        if self.cpu_only:
            payload["options"] = {"num_gpu": 0}
        if self.keep_alive is not None:
            payload["keep_alive"] = self.keep_alive

        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.exceptions.ConnectionError:
            raise EmbeddingError(
                f"Cannot connect to Ollama at {self.host}: server is not running or unreachable"
            )
        except requests.exceptions.Timeout:
            raise EmbeddingError(
                f"Ollama embedding request timed out after {self.timeout}s"
            )
        except Exception as exc:
            raise EmbeddingError(f"Ollama embedding request failed: {exc}")

        # Check for option rejection (e.g. 400 Bad Request when options provided)
        if resp.status_code == 400 and self.cpu_only and "options" in payload:
            CPU_OPTIONS_REJECTED = True
            payload_retry = {k: v for k, v in payload.items() if k != "options"}
            try:
                resp = requests.post(url, json=payload_retry, timeout=self.timeout)
            except Exception as exc:
                raise EmbeddingError(f"Ollama embedding retry failed: {exc}")

        if resp.status_code == 404:
            raise EmbeddingError(
                f"Embedding model '{self.model}' not found in Ollama. Run: ollama pull {self.model}"
            )
        if resp.status_code != 200:
            raise EmbeddingError(
                f"Ollama embedding request failed with status {resp.status_code}: {resp.text.strip()}"
            )

        try:
            data = resp.json()
        except Exception:
            raise EmbeddingError("Malformed response from Ollama embedding endpoint: invalid JSON")

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(batch):
            raise EmbeddingError(
                f"Malformed response from Ollama embedding endpoint: expected {len(batch)} embeddings"
            )

        return embeddings


DEFAULT_SYNONYMS: dict[str, str] = {
    "automobile": "car",
    "vehicle": "car",
    "vacation": "holiday",
    "leave": "holiday",
    "pto": "holiday",
    "physician": "doctor",
    "purchase": "buy",
    "telephone": "phone",
    "cellphone": "phone",
    "laptop": "computer",
    "notebook": "computer",
    "client": "customer",
    "guidelines": "policy",
    "rules": "policy",
    "regulations": "policy",
}


class FakeEmbedder(Embedder):
    """Deterministic, hashed bag-of-words vector embedder for tests and mock mode."""

    def __init__(self, dim: int = 64, synonyms: Optional[dict[str, str]] = None):
        self.dim = max(8, int(dim))
        self.synonyms = dict(DEFAULT_SYNONYMS)
        if synonyms:
            self.synonyms.update(synonyms)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts deterministically into unit-length float vectors."""
        results: list[list[float]] = []
        for text in texts:
            words = re.findall(r"[a-z0-9]+", str(text).lower())
            vec = [0.0] * self.dim
            for w in words:
                mapped = self.synonyms.get(w, w)
                idx = int(hashlib.md5(mapped.encode("utf-8")).hexdigest(), 16) % self.dim
                vec[idx] += 1.0

            if sum(vec) <= 0.0:
                # Text had no alphanumeric characters; hash the raw text
                idx = int(hashlib.md5(str(text).encode("utf-8")).hexdigest(), 16) % self.dim
                vec[idx] = 1.0

            results.append(_l2_normalize(vec))
        return results


# Per-process LRU cache for query embeddings
_QUERY_EMBEDDING_CACHE: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
_QUERY_CACHE_MAX_SIZE = 64


def get_cached_query_embedding(
    embedder: Embedder,
    query: str,
    timeout: float = 5.0,
) -> list[float]:
    """Retrieve or compute a cached query embedding vector with hard 5s timeout and LRU-64 cap."""
    model_name = getattr(embedder, "model", embedder.__class__.__name__)
    cache_key = (model_name, query.strip())

    if cache_key in _QUERY_EMBEDDING_CACHE:
        vec = _QUERY_EMBEDDING_CACHE.pop(cache_key)
        _QUERY_EMBEDDING_CACHE[cache_key] = vec
        return vec

    # Compute embedding with timeout
    vectors = embedder.embed([query])
    if not vectors or not vectors[0]:
        raise EmbeddingError("Empty embedding vector returned for query")

    vec = vectors[0]
    _QUERY_EMBEDDING_CACHE[cache_key] = vec
    if len(_QUERY_EMBEDDING_CACHE) > _QUERY_CACHE_MAX_SIZE:
        _QUERY_EMBEDDING_CACHE.popitem(last=False)

    return vec


def clear_query_embedding_cache() -> None:
    """Clear the in-memory query embedding cache (for testing isolation)."""
    _QUERY_EMBEDDING_CACHE.clear()
