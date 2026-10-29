"""Hits the real public endpoints once each. Off by default; run with ``pytest -m live``."""

from __future__ import annotations

import pytest

from omi import probe
from omi.calendar import last_completed_session, previous_session
from omi.config import PROJECT_ROOT, load_settings
from omi.http import HttpClient


@pytest.mark.live
def test_every_enabled_source_answers():
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    client = HttpClient(settings.network, max_attempts=2)
    rows = probe.run_probe(settings, client, previous_session(last_completed_session()))
    failed = [(r.source, r.result, r.error_kind, r.detail) for r in rows
              if r.result not in (probe.PASS, probe.SKIP)]
    assert not failed, failed
