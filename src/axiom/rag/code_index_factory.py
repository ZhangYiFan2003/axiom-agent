from __future__ import annotations

from pathlib import Path

from axiom.config import AxiomConfig
from axiom.rag.code_index import CodeIndex
from axiom.rag.provider_factory import create_embedding_provider_or_none


def create_code_index(cwd: str | Path, config: AxiomConfig) -> CodeIndex:
    provider = create_embedding_provider_or_none(config.embedding)
    return CodeIndex(cwd, embedding_provider=provider, search_config=config.embedding)
