from __future__ import annotations

from dataclasses import dataclass

from axiom.rag.chunker import _file_chunk, chunk_tree
from axiom.rag.languages import is_ast_language
from axiom.rag.models import CodeChunk, ImportBinding, SymbolDefinition, SymbolReference
from axiom.rag.parser import parse_source
from axiom.rag.symbols import extract_symbols


@dataclass(slots=True)
class FileAnalysis:
    chunks: list[CodeChunk]
    symbols: list[SymbolDefinition]
    imports: list[ImportBinding]
    references: list[SymbolReference]
    parse_status: str


def analyze_source(file_path: str, language: str, source: str) -> FileAnalysis:
    if not is_ast_language(language):
        return FileAnalysis(
            chunks=[_file_chunk(file_path, language, source, "unsupported", True)],
            symbols=[],
            imports=[],
            references=[],
            parse_status="unsupported",
        )
    source_bytes = source.encode("utf-8")
    try:
        tree = parse_source(language, source_bytes)
        chunks, parse_status = chunk_tree(file_path, language, source, source_bytes, tree)
    except Exception:
        chunks = [_file_chunk(file_path, language, source, "parse_error", True)]
        return FileAnalysis(chunks, [], [], [], "parse_error")
    extracted = extract_symbols(file_path, language, source, chunks)
    return FileAnalysis(
        chunks=chunks,
        symbols=extracted.definitions,
        imports=extracted.imports,
        references=extracted.references,
        parse_status=parse_status,
    )
