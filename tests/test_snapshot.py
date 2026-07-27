from __future__ import annotations

from axiom.snapshot import SnapshotService


def test_snapshot_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    file_path = project / "note.txt"
    file_path.write_text("before", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    file_path.write_text("after", encoding="utf-8")

    restored = service.restore(first.id)

    assert restored.id == first.id
    assert file_path.read_text(encoding="utf-8") == "before"

def test_snapshot_create_list_and_clean(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("saved", encoding="utf-8")
    pycache = project / "__pycache__"
    pycache.mkdir()
    (pycache / "ignored.pyc").write_bytes(b"cache")

    service = SnapshotService(project)
    pre = service.create("pre-turn")
    post = service.create("post-turn")

    records = service.list()

    assert [record.id for record in records] == [post.id, pre.id]
    assert (pre.path / "note.txt").read_text(encoding="utf-8") == "saved"
    assert not (pre.path / "__pycache__").exists()
    assert service.clean() == 2
    assert service.list() == []
