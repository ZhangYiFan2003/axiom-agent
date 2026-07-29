from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

from axiom.config import AxiomConfig
from axiom.rag import CodeIndex
from axiom.rag.call_graph import (
    CALL_EDGE_MIN_CONFIDENCE,
    build_call_edges,
    stable_call_edge_id,
)
from axiom.rag.models import SymbolDefinition, SymbolReference
from axiom.rag.store import SCHEMA_VERSION, CodeIndexStore
from axiom.tools.base import ToolContext
from axiom.tools.builtins import (
    find_callees,
    find_callers,
    find_recursive_components,
    trace_call_chain,
)

FIXTURE = Path(__file__).parent / "fixtures" / "call_graph_project"


def copy_fixture(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    return project


def definition(
    symbol_id: str,
    name: str,
    *,
    file_path: str = "a.py",
) -> SymbolDefinition:
    return SymbolDefinition(
        id=symbol_id,
        file_path=file_path,
        language="python",
        symbol_kind="function",
        name=name,
        qualified_name=name,
        container_symbol_id=None,
        container_qualified_name=None,
        signature=f"def {name}():",
        start_line=1,
        end_line=2,
        chunk_id=None,
        exported=True,
        visibility=None,
        definition_hash=symbol_id,
    )


def reference(
    reference_id: str,
    *,
    kind: str = "call",
    status: str = "resolved",
    confidence: float = 1.0,
    caller: str | None = "caller",
    callee: str | None = "callee",
    line: int = 3,
) -> SymbolReference:
    return SymbolReference(
        id=reference_id,
        file_path="a.py",
        language="python",
        reference_kind=kind,
        name="callee",
        qualifier=None,
        enclosing_symbol_id=caller,
        enclosing_qualified_name="caller" if caller else None,
        argument_count=0,
        start_line=line,
        end_line=line,
        resolved_symbol_id=callee,
        resolution_status=status,
        resolution_confidence=confidence,
        resolution_reason="test",
    )


def test_call_edge_identity_and_eligibility() -> None:
    definitions = [definition("caller", "caller"), definition("callee", "callee")]
    first = build_call_edges(definitions, [reference("ref-1")])
    moved = build_call_edges(definitions, [reference("ref-2", line=4)])
    changed_target = build_call_edges(
        [*definitions, definition("other", "other")],
        [reference("ref-1", callee="other")],
    )

    assert len(first.edges) == 1
    assert first.edges[0].id == stable_call_edge_id(
        reference_id="ref-1",
        caller_symbol_id="caller",
        callee_symbol_id="callee",
        edge_kind="call",
    )
    assert first.edges[0].id != moved.edges[0].id
    assert first.edges[0].id != changed_target.edges[0].id
    assert build_call_edges(definitions, [reference("ctor", kind="constructor_call")]).edges

    skipped = build_call_edges(
        definitions,
        [
            reference("member", kind="member_call"),
            reference("low", confidence=CALL_EDGE_MIN_CONFIDENCE - 0.01),
            reference("unresolved", status="unresolved", callee=None),
            reference("ambiguous", status="ambiguous", callee=None),
            reference("dynamic", status="dynamic", callee=None),
            reference("external", status="external", callee=None),
            reference("module", caller=None),
            reference("dangling-caller", caller="missing"),
            reference("dangling-callee", callee="missing"),
        ],
    )
    assert skipped.edges == []
    assert skipped.skipped_unsupported_kind == 1
    assert skipped.skipped_low_confidence == 1
    assert skipped.skipped_unresolved == 4
    assert skipped.skipped_missing_caller == 2
    assert skipped.skipped_missing_callee == 1

    duplicate = build_call_edges(definitions, [reference("ref-1"), reference("ref-1")])
    assert len(duplicate.edges) == 1


def test_schema_v6_migration_reaches_latest_in_one_initialization(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    index = CodeIndex(project, db_path=db_path)
    index.update()

    for version in ["5", "4", "3", "2"]:
        migrated = tmp_path / f"index-v{version}.sqlite3"
        shutil.copyfile(db_path, migrated)
        with sqlite3.connect(migrated) as conn:
            conn.execute(
                "update schema_metadata set value = ? where key = 'schema_version'",
                (version,),
            )
            if version in {"2", "3", "4", "5"}:
                conn.execute("drop table if exists call_edges")
            if version in {"2", "3", "4"}:
                conn.execute("drop table if exists symbol_references")
                conn.execute("drop table if exists import_bindings")
                conn.execute("drop table if exists symbol_definitions")
            if version in {"2", "3"}:
                conn.execute("drop table if exists chunk_embeddings")
                conn.execute("drop table if exists embedding_profiles")
            if version == "2":
                conn.execute("drop table if exists code_chunks_fts")
        store = CodeIndexStore(migrated)
        assert SCHEMA_VERSION == "6"
        assert store.schema_version() == "6"
        with sqlite3.connect(migrated) as conn:
            assert conn.execute(
                "select name from sqlite_master where name = 'call_edges'"
            ).fetchone()


def test_call_graph_queries_paths_and_recursion(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    stats = index.update()
    definitions = {item.qualified_name: item for item in index.store.list_symbol_definitions()}
    definitions_by_id = {item.id: item for item in index.store.list_symbol_definitions()}

    assert stats.call_edges_built > 0
    assert stats.call_edges_skipped_low_confidence >= 1
    assert stats.call_edges_skipped_unsupported_kind >= 1
    assert stats.recursive_components >= 2

    entry_callees = index.find_callees("entry")
    assert {definitions_by_id[edge.callee_symbol_id].qualified_name for edge in entry_callees} == {
        "alpha",
        "beta",
        "imported_target",
    }
    gamma_callers = index.find_callers("gamma")
    assert {definitions_by_id[edge.caller_symbol_id].qualified_name for edge in gamma_callers} == {
        "alpha",
        "beta",
    }

    outgoing = index.trace_call_paths("entry", max_depth=2)
    assert any(
        tuple(definitions_by_id[symbol].qualified_name for symbol in path.symbol_ids)
        == ("entry", "alpha", "gamma")
        for path in outgoing.paths
    )
    assert index.trace_call_paths("entry", max_depth=0).paths[0].symbol_ids == (
        definitions["entry"].id,
    )
    incoming = index.trace_call_paths("gamma", direction="incoming", max_depth=1)
    assert {path.symbol_ids[-1] for path in incoming.paths} == {
        definitions["alpha"].id,
        definitions["beta"].id,
    }

    components = index.find_recursive_components()
    kinds = {component.recursion_kind for component in components}
    assert {"direct", "mutual"} <= kinds
    assert any(component.symbol_ids == (definitions["recursive"].id,) for component in components)
    mutual_ids = {definitions["mutual_a"].id, definitions["mutual_b"].id}
    assert any(set(component.symbol_ids) == mutual_ids for component in components)

    weak_edges = [
        edge
        for edge in index.store.list_call_edges()
        if definitions_by_id[edge.caller_symbol_id].qualified_name == "weak_entry"
    ]
    assert weak_edges == []


def test_incremental_call_graph_updates(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    index = CodeIndex(project, db_path=db_path)
    index.update()
    graph_file = project / "python_pkg" / "graph.py"
    original = graph_file.read_text(encoding="utf-8")
    initial_edges = {edge.id for edge in index.store.list_call_edges()}

    graph_file.write_text(original.replace('return "done"', 'return "changed"'), encoding="utf-8")
    body_changed = index.update()
    assert body_changed.parsed_files == 1
    assert {edge.id for edge in index.store.list_call_edges()} == initial_edges

    graph_file.write_text(original.replace("def gamma", "def gamma_renamed"), encoding="utf-8")
    renamed = index.update()
    assert renamed.parsed_files == 1
    assert not index.find_callers("gamma_renamed")

    graph_file.write_text(original, encoding="utf-8")
    restored = index.update()
    assert restored.parsed_files == 1
    assert index.find_callers("gamma")
    imported_id = index.find_definitions("imported_target")[0].id

    helpers = project / "python_pkg" / "helpers.py"
    helpers.unlink()
    deleted = index.update()
    assert deleted.deleted_files == 1
    remaining_edges = index.store.list_call_edges()
    assert all(edge.callee_symbol_id != imported_id for edge in remaining_edges)


def test_call_graph_lookup_and_tools(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    index.update()

    duplicate_project_file = project / "python_pkg" / "dupe.py"
    duplicate_project_file.write_text(
        "def gamma():\n    return 'duplicate'\n",
        encoding="utf-8",
    )
    index.update()

    try:
        index.find_callers("gamma")
    except ValueError as exc:
        assert "ambiguous" in str(exc)
        assert exc.candidates
    else:
        raise AssertionError("expected ambiguous symbol lookup")

    context = ToolContext(cwd=str(project), config=AxiomConfig())
    CodeIndex(project).update()
    callers = asyncio.run(find_callers({"symbol": "imported_target"}, context))
    callees = asyncio.run(find_callees({"symbol": "entry"}, context))
    chain = asyncio.run(trace_call_chain({"symbol": "entry", "max_depth": 2}, context))
    recursive = asyncio.run(find_recursive_components({}, context))

    assert str(project) not in callers.content
    assert callers.is_error is False
    assert callees.is_error is False
    assert chain.is_error is False
    assert recursive.is_error is False
    assert "entry -> imported_target" in callers.content
    assert "entry -> alpha" in callees.content
    assert "entry -> alpha -> gamma" in chain.content
    assert "recursive" in recursive.content
