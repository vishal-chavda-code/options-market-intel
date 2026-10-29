from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from omi.config import PROJECT_ROOT, load_settings
from omi.pipeline import import_seed
from omi.quality import conform
from omi.sources.base import FetchRequest
from omi.sources.occ_daily_volume import DAILY
from omi.storage import IngestLog, Storage

D1, D2 = date(2026, 10, 27), date(2026, 10, 28)


def daily(d: date, equity: int, fetched: datetime) -> pd.DataFrame:
    return conform(DAILY, pd.DataFrame([{"trade_date": d, "equity": equity, "index_other": 1,
                                         "debt": 0, "futures": 0, "occ_total": equity + 1}]),
                   fetched)


def test_import_merges_tables_and_copies_missing_files(tmp_path, monkeypatch):
    seed = Storage(tmp_path / "seed")
    seed.upsert(DAILY, daily(D1, 100, datetime(2026, 10, 29, tzinfo=UTC)))
    seed.upsert(DAILY, daily(D2, 200, datetime(2026, 10, 29, tzinfo=UTC)))
    raw, _ = seed.write_raw(FetchRequest("occ_daily_volume", "r", D2, "u", ext="csv"), b"x")
    log = IngestLog(seed.meta_dir, "seedrun")
    log.add({"source": "occ_daily_volume", "report": "r", "as_of_date": D2, "key": "",
             "status": "ok", "bytes": 1, "rows_parsed": 2})
    log.flush()

    # The work machine already ran once: same year partition, newer fetch for D2.
    monkeypatch.setenv("OMI_PATHS__DATA_DIR", str(tmp_path / "work"))
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    work = Storage(settings.paths.data_dir)
    work.upsert(DAILY, daily(D2, 999, datetime(2026, 11, 2, tzinfo=UTC)))

    counts = import_seed(settings, seed.root)
    table = work.read_table("occ_daily_volume").set_index("trade_date")["equity"]
    assert table.to_dict() == {pd.Timestamp(D1): 100, pd.Timestamp(D2): 999}  # newer row kept
    assert counts["files_copied"] == 2  # one raw file + one ingest-log part
    assert (work.root / raw.relative_to(seed.root)).exists()
    ok = IngestLog(work.meta_dir, "check").ok_keys()
    assert ("occ_daily_volume", "r", D2, "") in ok

    again = import_seed(settings, seed.root)  # idempotent
    assert again["files_copied"] == 0
    assert len(work.read_table("occ_daily_volume")) == 2
