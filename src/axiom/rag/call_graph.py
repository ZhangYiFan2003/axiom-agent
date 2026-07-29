from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from axiom.rag.models import (
    CallEdge,
    CallPath,
    CallPathResult,
    RecursiveComponent,
    SymbolDefinition,
    SymbolReference,
)

CALL_EDGE_REFERENCE_KINDS = {"call", "constructor_call"}
CALL_EDGE_MIN_CONFIDENCE = 0.90
DEFAULT_MAX_DEPTH = 3
HARD_MAX_DEPTH = 10
DEFAULT_MAX_PATHS = 100
HARD_MAX_PATHS = 1000


@dataclass(slots=True)
class CallGraphBuildResult:
    edges: list[CallEdge]
    skipped_unresolved: int = 0
    skipped_low_confidence: int = 0
    skipped_unsupported_kind: int = 0
    skipped_missing_caller: int = 0
    skipped_missing_callee: int = 0


class SymbolLookupError(ValueError):
    def __init__(self, message: str, candidates: Sequence[SymbolDefinition] = ()):
        super().__init__(message)
        self.candidates = tuple(candidates)


def stable_call_edge_id(
    *,
    reference_id: str,
    caller_symbol_id: str,
    callee_symbol_id: str,
    edge_kind: str,
) -> str:
    payload = "|".join([reference_id, caller_symbol_id, callee_symbol_id, edge_kind])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_call_edges(
    definitions: Sequence[SymbolDefinition],
    references: Sequence[SymbolReference],
) -> CallGraphBuildResult:
    definitions_by_id = {definition.id: definition for definition in definitions}
    edges_by_id: dict[str, CallEdge] = {}
    result = CallGraphBuildResult(edges=[])
    for reference in sorted(
        references,
        key=lambda item: (item.file_path, item.start_line, item.id),
    ):
        if reference.reference_kind not in CALL_EDGE_REFERENCE_KINDS:
            result.skipped_unsupported_kind += 1
            continue
        if reference.resolution_status != "resolved" or not reference.resolved_symbol_id:
            result.skipped_unresolved += 1
            continue
        if reference.resolution_confidence < CALL_EDGE_MIN_CONFIDENCE:
            result.skipped_low_confidence += 1
            continue
        if not reference.enclosing_symbol_id:
            result.skipped_missing_caller += 1
            continue
        caller = definitions_by_id.get(reference.enclosing_symbol_id)
        if caller is None:
            result.skipped_missing_caller += 1
            continue
        callee = definitions_by_id.get(reference.resolved_symbol_id)
        if callee is None:
            result.skipped_missing_callee += 1
            continue
        edge_id = stable_call_edge_id(
            reference_id=reference.id,
            caller_symbol_id=caller.id,
            callee_symbol_id=callee.id,
            edge_kind=reference.reference_kind,
        )
        edges_by_id[edge_id] = CallEdge(
            id=edge_id,
            reference_id=reference.id,
            caller_symbol_id=caller.id,
            callee_symbol_id=callee.id,
            file_path=reference.file_path,
            language=reference.language,
            edge_kind=reference.reference_kind,
            start_line=reference.start_line,
            end_line=reference.end_line,
            resolution_confidence=reference.resolution_confidence,
            resolution_reason=reference.resolution_reason,
        )
    result.edges = sorted(
        edges_by_id.values(),
        key=lambda item: (item.file_path, item.start_line, item.caller_symbol_id, item.id),
    )
    return result


def resolve_symbol_query(
    symbol_id_or_name: str,
    definitions: Sequence[SymbolDefinition],
) -> SymbolDefinition:
    by_id = {definition.id: definition for definition in definitions}
    if symbol_id_or_name in by_id:
        return by_id[symbol_id_or_name]
    matches = sorted(
        [
            definition
            for definition in definitions
            if definition.name == symbol_id_or_name
            or definition.qualified_name == symbol_id_or_name
        ],
        key=lambda item: (item.file_path, item.start_line, item.qualified_name, item.id),
    )
    if not matches:
        raise SymbolLookupError(f'symbol "{symbol_id_or_name}" not found')
    if len(matches) > 1:
        raise SymbolLookupError(f'symbol "{symbol_id_or_name}" is ambiguous', matches)
    return matches[0]


def trace_call_paths(
    root_symbol_id: str,
    edges: Sequence[CallEdge],
    *,
    direction: str = "outgoing",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_paths: int = DEFAULT_MAX_PATHS,
) -> CallPathResult:
    if direction not in {"outgoing", "incoming"}:
        direction = "outgoing"
    bounded_depth = min(max(max_depth, 0), HARD_MAX_DEPTH)
    bounded_paths = min(max(max_paths, 1), HARD_MAX_PATHS)
    if bounded_depth == 0:
        return CallPathResult(
            root_symbol_id=root_symbol_id,
            direction=direction,
            max_depth=bounded_depth,
            paths=[CallPath((root_symbol_id,), (), False)],
            truncated=False,
        )

    adjacency = _adjacency(edges, direction=direction)
    paths: list[CallPath] = []
    truncated = False
    stack: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
        (root_symbol_id, (root_symbol_id,), ())
    ]
    while stack:
        current, symbol_path, edge_path = stack.pop()
        if len(edge_path) >= bounded_depth:
            continue
        for edge in reversed(adjacency.get(current, [])):
            next_symbol = (
                edge.callee_symbol_id if direction == "outgoing" else edge.caller_symbol_id
            )
            next_symbols = (*symbol_path, next_symbol)
            next_edges = (*edge_path, edge.id)
            cycle = next_symbol in symbol_path
            paths.append(CallPath(next_symbols, next_edges, cycle))
            if len(paths) >= bounded_paths:
                truncated = True
                return CallPathResult(root_symbol_id, direction, bounded_depth, paths, truncated)
            if not cycle:
                stack.append((next_symbol, next_symbols, next_edges))
    paths.sort(key=lambda item: (item.symbol_ids, item.edge_ids))
    return CallPathResult(root_symbol_id, direction, bounded_depth, paths, truncated)


def find_recursive_components(edges: Sequence[CallEdge]) -> list[RecursiveComponent]:
    adjacency: dict[str, set[str]] = {}
    edge_lookup: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.caller_symbol_id, set()).add(edge.callee_symbol_id)
        adjacency.setdefault(edge.callee_symbol_id, set())
        edge_lookup.setdefault((edge.caller_symbol_id, edge.callee_symbol_id), []).append(edge.id)

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[RecursiveComponent] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] != indices[node]:
            return
        members: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            members.append(member)
            if member == node:
                break
        member_set = set(members)
        is_direct = len(member_set) == 1 and (members[0], members[0]) in edge_lookup
        is_mutual = len(member_set) > 1
        if is_direct or is_mutual:
            component_edges = _component_edge_ids(member_set, adjacency, edge_lookup)
            components.append(
                RecursiveComponent(
                    symbol_ids=tuple(sorted(member_set)),
                    edge_ids=tuple(sorted(component_edges)),
                    recursion_kind="direct" if is_direct else "mutual",
                )
            )

    for node in sorted(adjacency):
        if node not in indices:
            strongconnect(node)

    return sorted(components, key=lambda item: (item.recursion_kind, item.symbol_ids))


def _component_edge_ids(
    members: set[str],
    adjacency: dict[str, set[str]],
    edge_lookup: dict[tuple[str, str], list[str]],
) -> list[str]:
    edge_ids: list[str] = []
    for caller in members:
        for callee in adjacency.get(caller, ()):
            if callee in members:
                edge_ids.extend(edge_lookup.get((caller, callee), ()))
    return edge_ids


def _adjacency(
    edges: Sequence[CallEdge],
    *,
    direction: str,
) -> dict[str, list[CallEdge]]:
    result: dict[str, list[CallEdge]] = {}
    for edge in edges:
        key = edge.caller_symbol_id if direction == "outgoing" else edge.callee_symbol_id
        result.setdefault(key, []).append(edge)
    for key, items in result.items():
        result[key] = sorted(
            items,
            key=lambda item: (item.file_path, item.start_line, item.callee_symbol_id, item.id),
        )
    return result
