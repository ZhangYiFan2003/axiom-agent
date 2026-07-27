from __future__ import annotations

from axiom.memory import MemoryManager


def test_memory_manager_persists_and_isolates_scopes(tmp_path):
    db_path = tmp_path / "memory.db"
    scope_a = str(tmp_path / "project-a")
    scope_b = str(tmp_path / "project-b")

    first = MemoryManager(db_path, scope=scope_a)
    first_id = first.save("alpha design decision")
    first.save("alpha runtime note")
    MemoryManager(db_path, scope=scope_b).save("beta private note")

    reloaded_a = MemoryManager(db_path, scope=scope_a)
    reloaded_b = MemoryManager(db_path, scope=scope_b)

    entries_a = reloaded_a.list()
    entries_b = reloaded_b.list()
    search_a = reloaded_a.search("alpha design")

    assert first_id == 1
    assert [entry.content for entry in entries_a] == [
        "alpha runtime note",
        "alpha design decision",
    ]
    assert [entry.content for entry in entries_b] == ["beta private note"]
    assert [entry.content for entry in search_a] == ["alpha design decision"]

    assert reloaded_b.clear() == 1
    assert reloaded_b.list() == []
    assert [entry.content for entry in reloaded_a.list()] == [
        "alpha runtime note",
        "alpha design decision",
    ]
