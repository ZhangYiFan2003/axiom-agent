from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from axiom.rag.models import CodeSearchResult
from axiom.rag.tokenizer import normalize_text, tokenize_query

# Positive values boost explainable matches after BM25 candidate recall.
# Negative values demote broad or fallback chunks so precise symbols win.
RANKING_WEIGHTS = {
    "exact_symbol": 8.0,
    "symbol_prefix": 4.0,
    "qualified_name": 3.0,
    "file_path": 1.5,
    "precise_chunk": 2.0,
    "file_chunk": -1.0,
    "fallback": -2.0,
}

PRECISE_CHUNK_TYPES = {
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


@dataclass(slots=True)
class RankedRow:
    row: dict[str, Any]
    score: float
    matched_fields: tuple[str, ...]


def rank_rows(rows: list[dict[str, Any]], query: str, limit: int) -> list[CodeSearchResult]:
    ranked = rank_candidate_rows(rows, query)
    deduped = _dedupe_ranked(ranked)
    return [_to_result(item) for item in deduped[:limit]]


def rank_candidate_rows(rows: list[dict[str, Any]], query: str) -> list[RankedRow]:
    query_tokens = tokenize_query(query)
    normalized_query = normalize_text(query).replace(" ", "")
    ranked = [_rank_row(row, normalized_query, query_tokens) for row in rows]
    ranked.sort(key=lambda item: (-item.score, item.row["file_path"], item.row["start_line"]))
    return ranked


def _rank_row(row: dict[str, Any], normalized_query: str, query_tokens: list[str]) -> RankedRow:
    score = float(row.get("bm25_score") or 0.0)
    matched_fields: list[str] = []

    symbol = normalize_text(row.get("symbol_name")).replace(" ", "")
    qualified = normalize_text(row.get("qualified_name")).replace(" ", "")
    path = normalize_text(row.get("file_path"))
    chunk_type = str(row.get("chunk_type") or "")

    if symbol and normalized_query == symbol:
        score += RANKING_WEIGHTS["exact_symbol"]
        matched_fields.append("exact_symbol")
    elif symbol and symbol.startswith(normalized_query):
        score += RANKING_WEIGHTS["symbol_prefix"]
        matched_fields.append("symbol_prefix")

    if qualified and any(token in qualified for token in query_tokens):
        score += RANKING_WEIGHTS["qualified_name"]
        matched_fields.append("qualified_name")

    if path and any(token in path for token in query_tokens):
        score += RANKING_WEIGHTS["file_path"]
        matched_fields.append("file_path")

    if chunk_type in PRECISE_CHUNK_TYPES:
        score += RANKING_WEIGHTS["precise_chunk"]
        matched_fields.append("precise_chunk")
    elif chunk_type == "file":
        score += RANKING_WEIGHTS["file_chunk"]

    if bool(row.get("is_fallback")):
        score += RANKING_WEIGHTS["fallback"]
        matched_fields.append("fallback")

    return RankedRow(row=row, score=score, matched_fields=tuple(matched_fields))


def _dedupe_ranked(items: list[RankedRow]) -> list[RankedRow]:
    best: dict[tuple[str, str, str], RankedRow] = {}
    ordered: list[RankedRow] = []
    for item in items:
        key = (
            str(item.row.get("file_path") or ""),
            str(item.row.get("qualified_name") or ""),
            str(item.row.get("content_hash") or ""),
        )
        previous = best.get(key)
        if previous is None:
            best[key] = item
            ordered.append(item)
            continue
        if item.score > previous.score:
            best[key] = item
            index = ordered.index(previous)
            ordered[index] = item
    ordered.sort(key=lambda item: (-item.score, item.row["file_path"], item.row["start_line"]))
    return ordered


def _to_result(item: RankedRow) -> CodeSearchResult:
    row = item.row
    return CodeSearchResult(
        path=str(row["file_path"]),
        line=int(row["start_line"]),
        snippet=_snippet(str(row["content"])),
        chunk_id=str(row.get("chunk_id") or "") or None,
        end_line=int(row["end_line"]) if row.get("end_line") is not None else None,
        content=str(row["content"]),
        chunk_type=str(row["chunk_type"]),
        symbol_name=row.get("symbol_name"),
        qualified_name=row.get("qualified_name"),
        score=item.score,
        backend=str(row.get("backend") or "like-fallback"),
        matched_fields=item.matched_fields,
        lexical_score=item.score,
        lexical_rank=int(row["lexical_rank"]) if row.get("lexical_rank") else None,
    )


def _snippet(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
