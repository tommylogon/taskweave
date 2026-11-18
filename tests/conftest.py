"""Shared pytest isolation fixtures so tests never mutate user ~/.taskweave state."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_taskweave_home(tmp_path, monkeypatch):
    home = tmp_path / ".taskweave_home"
    monkeypatch.setenv("TASKWEAVE_HOME", str(home))
    monkeypatch.delenv("TASKWEAVE_ROOT", raising=False)
    return home
