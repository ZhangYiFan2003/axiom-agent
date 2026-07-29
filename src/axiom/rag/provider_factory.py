from __future__ import annotations

from axiom.config import EmbeddingConfig
from axiom.rag.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
)


def create_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider | None:
    if not config.enabled:
        return None
    provider = config.provider.casefold()
    if provider != "openai-compatible":
        raise EmbeddingConfigurationError(f"unsupported embedding provider: {config.provider}")
    if not config.api_key:
        raise EmbeddingConfigurationError("AXIOM_EMBEDDING_API_KEY is not configured")
    if not config.model:
        raise EmbeddingConfigurationError("AXIOM_EMBEDDING_MODEL is not configured")
    if not config.base_url:
        raise EmbeddingConfigurationError("AXIOM_EMBEDDING_BASE_URL is not configured")
    return OpenAICompatibleEmbeddingProvider(
        provider_name=config.provider,
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        dimensions=config.dimensions,
        timeout=config.timeout,
        batch_size=config.batch_size,
    )


def create_embedding_provider_or_none(config: EmbeddingConfig) -> EmbeddingProvider | None:
    try:
        return create_embedding_provider(config)
    except EmbeddingConfigurationError:
        return None
