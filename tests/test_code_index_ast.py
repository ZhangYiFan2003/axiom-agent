from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

import pytest

from axiom.config import AxiomConfig
from axiom.rag import CodeIndex
from axiom.rag.store import SCHEMA_VERSION
from axiom.tools.base import ToolContext
from axiom.tools.builtins import search_code

FIXTURE = Path(__file__).parent / "fixtures" / "code_index_project"


def copy_fixture(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    return project


def rows(db_path: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        return conn.execute(query, params).fetchall()


def test_code_index_extracts_python_ast_chunks(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"

    stats = CodeIndex(project, db_path=db_path).update()

    assert stats.scanned_files == 5
    assert stats.indexed_files == 5
    chunks = rows(
        db_path,
        """
        select chunk_type, symbol_name, qualified_name, parent_symbol, start_line, end_line
        from code_chunks
        where file_path = 'app.py'
        order by start_line, chunk_type
        """,
    )
    names = {(row["chunk_type"], row["symbol_name"]) for row in chunks}
    assert ("file", "app.py") in names
    assert ("class", "Greeter") in names
    assert ("method", "greet") in names
    assert ("async_function", "load_user") in names
    greet = next(row for row in chunks if row["symbol_name"] == "greet")
    assert greet["qualified_name"] == "Greeter.greet"
    assert greet["parent_symbol"] == "Greeter"
    assert greet["start_line"] == 5
    assert greet["end_line"] == 6


def test_code_index_extracts_java_and_typescript_chunks(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"

    CodeIndex(project, db_path=db_path).update()

    java_names = {
        (row["chunk_type"], row["symbol_name"], row["qualified_name"])
        for row in rows(
            db_path,
            "select chunk_type, symbol_name, qualified_name from code_chunks where file_path = ?",
            ("Service.java",),
        )
    }
    assert ("class", "Service", "Service") in java_names
    assert ("constructor", "Service", "Service.Service") in java_names
    assert ("method", "greet", "Service.greet") in java_names
    assert ("interface", "Worker", "Worker") in java_names

    ts_names = {
        (row["chunk_type"], row["symbol_name"], row["qualified_name"])
        for row in rows(
            db_path,
            "select chunk_type, symbol_name, qualified_name from code_chunks where file_path = ?",
            ("client.ts",),
        )
    }
    assert ("interface", "ApiClient", "ApiClient") in ts_names
    assert ("type", "UserId", "UserId") in ts_names
    assert ("class", "HttpClient", "HttpClient") in ts_names
    assert ("method", "fetchUser", "HttpClient.fetchUser") in ts_names
    assert ("arrow_function", "normalizeUser", "normalizeUser") in ts_names
    assert ("function", "buildClient", "buildClient") in ts_names


def test_code_index_records_fallback_and_schema_version(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"

    CodeIndex(project, db_path=db_path).rebuild()

    metadata = rows(db_path, "select value from schema_metadata where key = 'schema_version'")
    assert metadata[0]["value"] == SCHEMA_VERSION
    fallback = rows(
        db_path,
        """
        select chunk_type, is_fallback, parse_status
        from code_chunks
        where file_path = 'notes.txt'
        """,
    )
    assert [(row["chunk_type"], row["is_fallback"], row["parse_status"]) for row in fallback] == [
        ("file", 1, "unsupported")
    ]
    broken = rows(
        db_path,
        "select parse_status from indexed_files where path = 'broken.py'",
    )
    assert broken[0]["parse_status"] == "parsed_with_errors"


def test_code_index_incremental_update_add_and_delete(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    index = CodeIndex(project, db_path=db_path)

    first = index.update()
    second = index.update()
    assert first.indexed_files == 5
    assert second.indexed_files == 0
    assert second.unchanged_files == 5

    app = project / "app.py"
    updated_app = app.read_text(encoding="utf-8") + "\ndef added():\n    return 1\n"
    app.write_text(updated_app, encoding="utf-8")
    modified = index.update()
    assert modified.indexed_files == 1
    assert modified.unchanged_files == 4
    assert rows(db_path, "select symbol_name from code_chunks where symbol_name = 'added'")

    (project / "extra.js").write_text("export function extra() { return 1; }\n", encoding="utf-8")
    added = index.update()
    assert added.indexed_files == 1
    assert added.unchanged_files == 5

    (project / "Service.java").unlink()
    deleted = index.update()
    assert deleted.deleted_files == 1
    assert not rows(db_path, "select path from indexed_files where path = 'Service.java'")


def test_code_index_rebuilds_old_schema(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table code_chunks (
                id integer primary key autoincrement,
                root text not null,
                path text not null,
                line integer not null,
                content text not null
            )
            """
        )
        conn.execute(
            "insert into code_chunks(root, path, line, content) values (?, ?, ?, ?)",
            (str(project), "old.py", 1, "legacy"),
        )

    index = CodeIndex(project, db_path=db_path)

    assert index.store.schema_version() == SCHEMA_VERSION
    columns = {row["name"] for row in rows(db_path, "pragma table_info(code_chunks)")}
    assert "file_path" in columns
    assert "root" not in columns


def test_code_index_search_api_and_tool_are_backward_compatible(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    index = CodeIndex(project, db_path=db_path)
    count = index.rebuild()

    assert count > 0
    results = index.search("Greeter greet", limit=5)
    assert results
    assert results[0].path
    assert isinstance(results[0].line, int)
    assert results[0].snippet

    (project / ".axiom").mkdir()
    shutil.copyfile(db_path, project / ".axiom" / "code_index.sqlite3")
    output = asyncio.run(
        search_code(
            {"query": "Greeter greet", "limit": 5},
            ToolContext(cwd=str(project), config=AxiomConfig()),
        )
    )
    assert "app.py:" in output.content


def test_code_index_rejects_paths_outside_workspace(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("def leaked():\n    pass\n", encoding="utf-8")

    with pytest.raises(ValueError):
        CodeIndex(project, db_path=tmp_path / "index.sqlite3").rebuild(outside)
