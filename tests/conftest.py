from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_bytes():
    def load(rel: str) -> bytes:
        return (FIXTURES / rel).read_bytes()

    return load


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Keep the developer's own OMI_* / FRED settings from leaking into tests."""
    import os

    for name in list(os.environ):
        if name.startswith("OMI_") and name != "OMI_HOME":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
