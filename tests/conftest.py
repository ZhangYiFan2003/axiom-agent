from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_user_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", home.drive or "C:")
    monkeypatch.setenv("HOMEPATH", os.sep + str(home).split(":\\", 1)[-1])
