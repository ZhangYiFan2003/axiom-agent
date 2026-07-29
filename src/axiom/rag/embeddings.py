from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from axiom.rag.models import CodeChunk

EMBEDDING_INPUT_VERSION = "1"
NORMALIZATION_VERSION = "l2-v1"
VECTOR_FORMAT_VERSION = "float32-le-v1"

EMBEDDABLE_CHUNK_TYPES = {
    "class",
    "interface",
    "enum",
    "constructor",
    "method",
    "async_method",
    "function",
    "async_function",
    "arrow_function",
    "type",
}


class EmbeddingError(RuntimeError):
    pass


class EmbeddingConfigurationError(EmbeddingError):
    pass


class EmbeddingProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def dimensions(self) -> int | None: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(slots=True)
class EmbeddingProfile:
    id: str
    provider: str
    model: str
    dimensions: int
    input_version: str = EMBEDDING_INPUT_VERSION
    vector_format: str = VECTOR_FORMAT_VERSION


@dataclass(slots=True)
class OpenAICompatibleEmbeddingProvider:
    provider_name: str
    model: str
    api_key: str
    base_url: str
    dimensions: int | None = None
    timeout: float = 60.0
    batch_size: int = 64
    max_retries: int = 2
    transport: httpx.BaseTransport | None = None

    @property
    def model_name(self) -> str:
        return self.model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.api_key:
            raise EmbeddingConfigurationError("embedding API key is not configured")
        if not self.model:
            raise EmbeddingConfigurationError("embedding model is not configured")
        if not self.base_url:
            raise EmbeddingConfigurationError("embedding base_url is not configured")

        results: list[list[float]] = []
        batch_size = max(1, int(self.batch_size))
        for offset in range(0, len(texts), batch_size):
            batch = list(texts[offset : offset + batch_size])
            results.extend(self._embed_batch(batch))
        _validate_embeddings(results, expected_count=len(texts), dimensions=self.dimensions)
        return results

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self.model, "input": texts}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "user-agent": "axiom-agent/0.1.0",
        }
        url = self.base_url.rstrip("/") + "/embeddings"
        retry = 0
        while True:
            try:
                with httpx.Client(
                    timeout=self.timeout,
                    http2=False,
                    transport=self.transport,
                ) as client:
                    response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                return _parse_embedding_response(data, len(texts), self.dimensions)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500:
                    raise EmbeddingError(f"embedding endpoint returned HTTP {status}") from exc
                if retry >= self.max_retries:
                    raise EmbeddingError(f"embedding endpoint returned HTTP {status}") from exc
            except (httpx.HTTPError, ValueError) as exc:
                if retry >= self.max_retries:
                    raise EmbeddingError("embedding request failed") from exc
            retry += 1
            time.sleep(min(0.25 * retry, 1.0))


def build_embedding_text(chunk: CodeChunk, *, max_chars: int = 12000) -> str:
    fields = [
        f"version: {EMBEDDING_INPUT_VERSION}",
        f"language: {chunk.language}",
        f"file_path: {chunk.file_path}",
        f"chunk_type: {chunk.chunk_type}",
        f"symbol_name: {chunk.symbol_name or ''}",
        f"qualified_name: {chunk.qualified_name or ''}",
        f"parent_symbol: {chunk.parent_symbol or ''}",
        "content:",
        chunk.content,
    ]
    text = "\n".join(fields)
    if max_chars > 0 and len(text) > max_chars:
        return text[:max_chars]
    return text


def embedding_input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_embedding_eligible(chunk: CodeChunk) -> bool:
    if chunk.is_fallback:
        return True
    return chunk.chunk_type in EMBEDDABLE_CHUNK_TYPES


def build_embedding_profile(provider: EmbeddingProvider, *, dimensions: int) -> EmbeddingProfile:
    source = "\n".join(
        [
            provider.provider_name,
            provider.model_name,
            str(dimensions),
            EMBEDDING_INPUT_VERSION,
            NORMALIZATION_VERSION,
            VECTOR_FORMAT_VERSION,
        ]
    )
    profile_id = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return EmbeddingProfile(
        id=profile_id,
        provider=provider.provider_name,
        model=provider.model_name,
        dimensions=dimensions,
    )


def infer_dimensions(provider: EmbeddingProvider, sample_text: str) -> int:
    if provider.dimensions is not None:
        return provider.dimensions
    vectors = provider.embed([sample_text])
    _validate_embeddings(vectors, expected_count=1, dimensions=None)
    return len(vectors[0])


def _parse_embedding_response(
    data: dict[str, Any],
    expected_count: int,
    dimensions: int | None,
) -> list[list[float]]:
    items = data.get("data")
    if not isinstance(items, list):
        raise EmbeddingError("embedding response missing data list")
    ordered: list[list[float] | None] = [None] * expected_count
    for item in items:
        if not isinstance(item, dict):
            raise EmbeddingError("embedding response item must be an object")
        index = int(item.get("index"))
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise EmbeddingError("embedding response item missing embedding")
        if index < 0 or index >= expected_count:
            raise EmbeddingError("embedding response index is out of range")
        ordered[index] = [float(value) for value in embedding]
    if any(vector is None for vector in ordered):
        raise EmbeddingError("embedding response did not include every input")
    result = [vector for vector in ordered if vector is not None]
    _validate_embeddings(result, expected_count=expected_count, dimensions=dimensions)
    return result


def _validate_embeddings(
    vectors: Sequence[Sequence[float]],
    *,
    expected_count: int,
    dimensions: int | None,
) -> None:
    if len(vectors) != expected_count:
        raise EmbeddingError(f"expected {expected_count} embeddings, got {len(vectors)}")
    expected_dimensions = dimensions
    for vector in vectors:
        if not vector:
            raise EmbeddingError("embedding vector must not be empty")
        if any(not math.isfinite(float(value)) for value in vector):
            raise EmbeddingError("embedding vector must contain only finite values")
        if expected_dimensions is None:
            expected_dimensions = len(vector)
        elif len(vector) != expected_dimensions:
            raise EmbeddingError(
                f"expected {expected_dimensions} embedding dimensions, got {len(vector)}"
            )
