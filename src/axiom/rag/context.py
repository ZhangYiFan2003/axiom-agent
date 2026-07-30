from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from axiom.rag.call_graph import SymbolLookupError, resolve_symbol_query, trace_call_paths
from axiom.rag.models import (
    CallEdge,
    CodeChunk,
    CodeContextItem,
    CodeContextResult,
    SymbolDefinition,
)
from axiom.rag.ranking import PRECISE_CHUNK_TYPES

if TYPE_CHECKING:
    from axiom.rag.code_index import CodeIndex

DEFAULT_MAX_CONTEXT_CHARS = 24_000
DEFAULT_MAX_ESTIMATED_TOKENS = 6_000
DEFAULT_MAX_SEED_CHUNKS = 8
DEFAULT_MAX_GRAPH_DEPTH = 2
DEFAULT_MAX_CONTEXT_ITEMS = 30
HARD_MAX_CONTEXT_CHARS = 48_000
HARD_MAX_ESTIMATED_TOKENS = 12_000
HARD_MAX_SEED_CHUNKS = 20
HARD_MAX_GRAPH_DEPTH = 4
HARD_MAX_CONTEXT_ITEMS = 60

CONTEXT_REASONS = {
    "search_seed",
    "symbol_definition",
    "direct_reference",
    "caller",
    "callee",
    "incoming_path",
    "outgoing_path",
}

RESULT_METADATA_RESERVE_CHARS = 256

REASON_PRIORITY = {
    "search_seed": 0,
    "symbol_definition": 1,
    "caller": 2,
    "callee": 2,
    "direct_reference": 3,
    "incoming_path": 4,
    "outgoing_path": 4,
}


def estimate_tokens(text: str) -> int:
    """Estimate context cost with a deterministic local heuristic.

    This is intentionally not a model-specific tokenizer. It approximates code
    as one token per four characters, with a small line-count floor so dense
    short lines still consume budget.
    """
    if not text:
        return 0
    char_estimate = (len(text) + 3) // 4
    line_estimate = len(text.splitlines())
    return max(1, char_estimate, line_estimate)


def serialized_item_text(item: CodeContextItem) -> str:
    symbol = item.symbol_id or "-"
    seed = "-" if item.seed_rank is None else str(item.seed_rank)
    return "\n".join(
        [
            (
                f"reason={item.reason} path={item.file_path}:{item.start_line}-{item.end_line} "
                f"symbol={symbol} seed_rank={seed} graph_distance={item.graph_distance} "
                f"estimated_tokens={item.estimated_tokens}"
            ),
            item.content.rstrip(),
            "",
        ]
    )


def serialized_item_chars(item: CodeContextItem) -> int:
    return len(serialized_item_text(item))


class CodeContextBuilder:
    def __init__(self, index: CodeIndex):
        self.index = index

    def build(
        self,
        query: str,
        *,
        mode: str = "auto",
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        max_estimated_tokens: int = DEFAULT_MAX_ESTIMATED_TOKENS,
        max_seed_chunks: int = DEFAULT_MAX_SEED_CHUNKS,
        max_graph_depth: int = DEFAULT_MAX_GRAPH_DEPTH,
        max_items: int = DEFAULT_MAX_CONTEXT_ITEMS,
    ) -> CodeContextResult:
        max_context_chars = _bounded(max_context_chars, 1, HARD_MAX_CONTEXT_CHARS)
        max_estimated_tokens = _bounded(
            max_estimated_tokens,
            1,
            HARD_MAX_ESTIMATED_TOKENS,
        )
        max_seed_chunks = _bounded(max_seed_chunks, 1, HARD_MAX_SEED_CHUNKS)
        max_graph_depth = _bounded(max_graph_depth, 0, HARD_MAX_GRAPH_DEPTH)
        max_items = _bounded(max_items, 1, HARD_MAX_CONTEXT_ITEMS)

        chunks = self.index.store.list_chunks()
        definitions = self.index.store.list_symbol_definitions()
        references = self.index.store.list_symbol_references()
        edges = self.index.store.list_call_edges()
        chunks_by_id = {chunk.id: chunk for chunk in chunks}
        definitions_by_id = {definition.id: definition for definition in definitions}
        definitions_by_chunk = {
            definition.chunk_id: definition
            for definition in definitions
            if definition.chunk_id is not None
        }

        candidates: list[CodeContextItem] = []
        seed_symbol_ids: dict[str, int | None] = {}
        seed_results = self.index.search(query, max_seed_chunks, mode=mode)
        for seed_rank, result in enumerate(seed_results, start=1):
            chunk = _search_result_chunk(result.chunk_id, result.path, result.line, chunks)
            if chunk is None:
                continue
            symbol = _symbol_for_chunk(chunk, definitions_by_chunk, definitions)
            if symbol is not None:
                seed_symbol_ids.setdefault(symbol.id, seed_rank)
            candidates.append(_item(chunk, symbol, "search_seed", seed_rank, 0))

        try:
            symbol = resolve_symbol_query(query, definitions)
        except SymbolLookupError:
            symbol = None
        if symbol is not None:
            seed_symbol_ids.setdefault(symbol.id, 0)
        for symbol in _mentioned_unique_symbols(query, definitions):
            seed_symbol_ids.setdefault(symbol.id, len(seed_symbol_ids) + 1)

        for symbol_id, seed_rank in sorted(
            seed_symbol_ids.items(),
            key=lambda item: (item[1] or 0, item[0]),
        ):
            symbol = definitions_by_id.get(symbol_id)
            if symbol is None:
                continue
            self._add_symbol_definition(
                candidates,
                symbol,
                chunks_by_id,
                definitions,
                reason="symbol_definition",
                seed_rank=seed_rank,
                graph_distance=0,
            )
            if max_graph_depth == 0:
                continue
            self._add_direct_references(
                candidates,
                symbol,
                references,
                chunks,
                definitions,
                seed_rank=seed_rank,
            )
            self._add_direct_edges(
                candidates,
                symbol,
                edges,
                definitions_by_id,
                chunks_by_id,
                definitions,
                seed_rank=seed_rank,
            )
            self._add_paths(
                candidates,
                symbol,
                edges,
                definitions_by_id,
                chunks_by_id,
                definitions,
                seed_rank=seed_rank,
                max_graph_depth=max_graph_depth,
            )

        ordered = _dedupe_and_sort(candidates)
        items: list[CodeContextItem] = []
        used_chars = min(RESULT_METADATA_RESERVE_CHARS + len(query), max_context_chars)
        used_tokens = estimate_tokens("x" * used_chars)
        truncated = False
        for item in ordered:
            if len(items) >= max_items:
                truncated = True
                break
            item_chars = serialized_item_chars(item)
            next_chars = used_chars + item_chars
            next_tokens = used_tokens + item.estimated_tokens
            if next_chars > max_context_chars or next_tokens > max_estimated_tokens:
                truncated = True
                continue
            items.append(item)
            used_chars = next_chars
            used_tokens = next_tokens

        expanded_symbol_count = len(
            {
                item.symbol_id
                for item in items
                if item.symbol_id and item.reason != "search_seed"
            }
        )
        return CodeContextResult(
            query=query,
            items=items,
            estimated_chars=used_chars,
            estimated_tokens=used_tokens,
            truncated=truncated,
            seed_count=len(seed_results),
            expanded_symbol_count=expanded_symbol_count,
            max_context_chars=max_context_chars,
            max_estimated_tokens=max_estimated_tokens,
        )

    def _add_symbol_definition(
        self,
        candidates: list[CodeContextItem],
        symbol: SymbolDefinition,
        chunks_by_id: dict[str, CodeChunk],
        definitions: list[SymbolDefinition],
        *,
        reason: str,
        seed_rank: int | None,
        graph_distance: int,
    ) -> None:
        chunk = chunks_by_id.get(symbol.chunk_id or "") or _chunk_for_symbol(
            symbol,
            self.index.store.list_chunks(),
        )
        if chunk is not None:
            candidates.append(_item(chunk, symbol, reason, seed_rank, graph_distance))

    def _add_direct_references(
        self,
        candidates: list[CodeContextItem],
        symbol: SymbolDefinition,
        references,
        chunks: list[CodeChunk],
        definitions: list[SymbolDefinition],
        *,
        seed_rank: int | None,
    ) -> None:
        for reference in references:
            if reference.resolved_symbol_id != symbol.id:
                continue
            chunk = _smallest_chunk_covering(
                chunks,
                reference.file_path,
                reference.start_line,
                reference.end_line,
            )
            if chunk is None:
                continue
            item_symbol = _symbol_for_chunk(chunk, {}, definitions)
            candidates.append(_item(chunk, item_symbol, "direct_reference", seed_rank, 1))

    def _add_direct_edges(
        self,
        candidates: list[CodeContextItem],
        symbol: SymbolDefinition,
        edges: list[CallEdge],
        definitions_by_id: dict[str, SymbolDefinition],
        chunks_by_id: dict[str, CodeChunk],
        definitions: list[SymbolDefinition],
        *,
        seed_rank: int | None,
    ) -> None:
        for edge in edges:
            if edge.callee_symbol_id == symbol.id:
                caller = definitions_by_id.get(edge.caller_symbol_id)
                if caller is not None:
                    self._add_symbol_definition(
                        candidates,
                        caller,
                        chunks_by_id,
                        definitions,
                        reason="caller",
                        seed_rank=seed_rank,
                        graph_distance=1,
                    )
            if edge.caller_symbol_id == symbol.id:
                callee = definitions_by_id.get(edge.callee_symbol_id)
                if callee is not None:
                    self._add_symbol_definition(
                        candidates,
                        callee,
                        chunks_by_id,
                        definitions,
                        reason="callee",
                        seed_rank=seed_rank,
                        graph_distance=1,
                    )

    def _add_paths(
        self,
        candidates: list[CodeContextItem],
        symbol: SymbolDefinition,
        edges: list[CallEdge],
        definitions_by_id: dict[str, SymbolDefinition],
        chunks_by_id: dict[str, CodeChunk],
        definitions: list[SymbolDefinition],
        *,
        seed_rank: int | None,
        max_graph_depth: int,
    ) -> None:
        if max_graph_depth < 2:
            return
        for direction, reason in (("incoming", "incoming_path"), ("outgoing", "outgoing_path")):
            result = trace_call_paths(
                symbol.id,
                edges,
                direction=direction,
                max_depth=max_graph_depth,
                max_paths=100,
            )
            for path in result.paths:
                for distance, path_symbol_id in enumerate(path.symbol_ids[1:], start=1):
                    if distance < 2:
                        continue
                    path_symbol = definitions_by_id.get(path_symbol_id)
                    if path_symbol is None:
                        continue
                    self._add_symbol_definition(
                        candidates,
                        path_symbol,
                        chunks_by_id,
                        definitions,
                        reason=reason,
                        seed_rank=seed_rank,
                        graph_distance=distance,
                    )


def _item(
    chunk: CodeChunk,
    symbol: SymbolDefinition | None,
    reason: str,
    seed_rank: int | None,
    graph_distance: int,
) -> CodeContextItem:
    item = CodeContextItem(
        chunk_id=chunk.id,
        symbol_id=symbol.id if symbol else None,
        file_path=chunk.file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        content=chunk.content,
        reason=reason,
        seed_rank=seed_rank,
        graph_distance=graph_distance,
        estimated_tokens=0,
    )
    return replace(item, estimated_tokens=estimate_tokens(serialized_item_text(item)))


def _search_result_chunk(
    chunk_id: str | None,
    path: str,
    line: int,
    chunks: list[CodeChunk],
) -> CodeChunk | None:
    if chunk_id:
        for chunk in chunks:
            if chunk.id == chunk_id:
                return chunk
    matches = [
        chunk
        for chunk in chunks
        if chunk.file_path == path and chunk.start_line == line
    ]
    if not matches:
        return None
    return sorted(matches, key=_specificity_key)[0]


def _mentioned_unique_symbols(
    query: str,
    definitions: list[SymbolDefinition],
) -> list[SymbolDefinition]:
    normalized_query = _compact(query)
    if not normalized_query:
        return []
    by_key: dict[str, list[SymbolDefinition]] = {}
    for definition in definitions:
        for key in {_compact(definition.name), _compact(definition.qualified_name)}:
            if key:
                by_key.setdefault(key, []).append(definition)
    matches: dict[str, SymbolDefinition] = {}
    for key, items in by_key.items():
        unique = {item.id: item for item in items}
        if len(unique) != 1 or key not in normalized_query:
            continue
        symbol = next(iter(unique.values()))
        matches[symbol.id] = symbol
    return sorted(matches.values(), key=lambda item: (item.file_path, item.start_line, item.id))


def _compact(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _symbol_for_chunk(
    chunk: CodeChunk,
    definitions_by_chunk: dict[str, SymbolDefinition],
    definitions: list[SymbolDefinition],
) -> SymbolDefinition | None:
    direct = definitions_by_chunk.get(chunk.id)
    if direct is not None:
        return direct
    matches = [
        definition
        for definition in definitions
        if definition.file_path == chunk.file_path
        and definition.start_line <= chunk.start_line
        and chunk.end_line <= definition.end_line
    ]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda item: (
            item.end_line - item.start_line,
            item.start_line,
            item.qualified_name,
            item.id,
        ),
    )[0]


def _chunk_for_symbol(symbol: SymbolDefinition, chunks: list[CodeChunk]) -> CodeChunk | None:
    matches = [
        chunk
        for chunk in chunks
        if chunk.file_path == symbol.file_path
        and chunk.start_line <= symbol.start_line
        and symbol.end_line <= chunk.end_line
    ]
    if not matches:
        return None
    return sorted(matches, key=_specificity_key)[0]


def _smallest_chunk_covering(
    chunks: list[CodeChunk],
    file_path: str,
    start_line: int,
    end_line: int,
) -> CodeChunk | None:
    matches = [
        chunk
        for chunk in chunks
        if chunk.file_path == file_path
        and chunk.start_line <= start_line
        and end_line <= chunk.end_line
    ]
    if not matches:
        return None
    return sorted(matches, key=_specificity_key)[0]


def _dedupe_and_sort(candidates: list[CodeContextItem]) -> list[CodeContextItem]:
    best_by_chunk: dict[str, CodeContextItem] = {}
    best_by_range: dict[tuple[str, int, int], CodeContextItem] = {}
    best_by_content: dict[tuple[str, str], CodeContextItem] = {}
    for item in candidates:
        current = _best(item, best_by_chunk.get(item.chunk_id))
        best_by_chunk[item.chunk_id] = current
    for item in best_by_chunk.values():
        key = (item.file_path, item.start_line, item.end_line)
        best_by_range[key] = _best(item, best_by_range.get(key))
    for item in best_by_range.values():
        key = (item.file_path, item.content)
        best_by_content[key] = _best(item, best_by_content.get(key))
    return sorted(best_by_content.values(), key=_rank_key)


def _best(item: CodeContextItem, previous: CodeContextItem | None) -> CodeContextItem:
    if previous is None:
        return item
    if _rank_key(item) < _rank_key(previous):
        return item
    if _is_more_specific(item, previous):
        return replace(item, reason=previous.reason, seed_rank=previous.seed_rank)
    return previous


def _rank_key(item: CodeContextItem) -> tuple[object, ...]:
    seed_rank = item.seed_rank if item.seed_rank is not None else 10**9
    return (
        REASON_PRIORITY.get(item.reason, 99),
        seed_rank,
        item.graph_distance,
        item.file_path,
        item.start_line,
        item.end_line,
        item.symbol_id or "",
        item.chunk_id,
    )


def _specificity_key(chunk: CodeChunk) -> tuple[object, ...]:
    precise = 0 if chunk.chunk_type in PRECISE_CHUNK_TYPES else 1
    return (
        precise,
        chunk.end_line - chunk.start_line,
        chunk.start_line,
        chunk.chunk_type,
        chunk.id,
    )


def _is_more_specific(item: CodeContextItem, previous: CodeContextItem) -> bool:
    return (item.end_line - item.start_line, item.chunk_id) < (
        previous.end_line - previous.start_line,
        previous.chunk_id,
    )


def _bounded(value: int, lower: int, upper: int) -> int:
    return min(max(int(value), lower), upper)
