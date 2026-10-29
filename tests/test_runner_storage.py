from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from omi.config import PROJECT_ROOT, load_settings
from omi.http import FetchResult
from omi.quality import conform
from omi.runner import run_source, run_sources
from omi.sources.base import FetchRequest, NotAvailable, RunContext, SchemaDrift, TableSpec
from omi.storage import IngestLog, Storage

SPEC = TableSpec("toy", {"trade_date": "date", "sym": "string", "v": "int64"},
                 pk=("trade_date", "sym"), partition_col="trade_date", partition="month")
D1, D2 = date(2026, 10, 27), date(2026, 10, 28)


class ToySource:
    """Body text decides the outcome: 'NA' not available, 'DRIFT' schema drift, else rows."""

    name = "toy"
    tables = (SPEC,)

    def plan(self, dates, ctx):
        return [FetchRequest("toy", "r", d, f"https://toy.test/{d}") for d in dates]

    def parse(self, req, raw):
        if raw == b"NA":
            raise NotAvailable("not yet")
        if raw == b"DRIFT":
            raise SchemaDrift("layout changed")
        return {"toy": pd.DataFrame({"trade_date": [req.as_of] * 2, "sym": ["A", "B"],
                                     "v": [int(raw), int(raw) + 1]})}


class CrashingSource(ToySource):
    name = "crash"

    def plan(self, dates, ctx):
        raise RuntimeError("boom")


class FakeHttp:
    def __init__(self, bodies: dict[date, bytes]):
        self.bodies = bodies
        self.calls: list[str] = []

    def get(self, url):
        self.calls.append(url)
        d = date.fromisoformat(url.rsplit("/", 1)[1])
        return FetchResult(url=url, status=200, content=self.bodies[d], headers={},
                           final_url=url, elapsed_s=0.0, attempts=1)


@pytest.fixture
def env(tmp_path):
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    storage = Storage(tmp_path / "data")
    ctx = RunContext(settings, D2)

    def run(source, bodies, *, force=False, commit_every=1):
        http = FakeHttp(bodies)
        ingest = IngestLog(storage.meta_dir, f"run{len(list(storage.meta_dir.rglob('*')))}")
        summary = run_source(source, sorted(bodies), ctx, http=http, storage=storage,
                             ingest=ingest, force=force, commit_every=commit_every)
        return summary, http, ingest

    return storage, run


def test_rerun_is_a_no_op(env):
    storage, run = env
    first, http1, _ = run(ToySource(), {D1: b"1", D2: b"5"})
    assert (first.ok, first.rows, len(http1.calls)) == (2, 4, 2)
    before = storage.read_table("toy")
    second, http2, ingest = run(ToySource(), {D1: b"1", D2: b"5"})
    assert (second.skipped, second.ok, len(http2.calls)) == (2, 0, 0)
    pd.testing.assert_frame_equal(storage.read_table("toy"), before)
    assert len(ingest.read()) == 2  # skips are not logged as attempts


def test_force_refetches_and_last_write_wins(env):
    storage, run = env
    run(ToySource(), {D2: b"5"})
    run(ToySource(), {D2: b"9"}, force=True)
    df = storage.read_table("toy").set_index("sym")
    assert df.loc["A", "v"] == 9 and len(df) == 2


def test_not_available_is_retried_next_run(env):
    storage, run = env
    first, _, ingest = run(ToySource(), {D2: b"NA"})
    assert (first.ok, first.not_available) == (0, 1)
    assert not storage.has_table("toy")
    assert ingest.read()["status"].tolist() == ["not_available"]
    second, http, _ = run(ToySource(), {D2: b"3"})
    assert second.ok == 1 and len(http.calls) == 1


def test_schema_drift_keeps_raw_and_writes_no_rows(env):
    storage, run = env
    summary, _, ingest = run(ToySource(), {D1: b"DRIFT", D2: b"2"})
    assert (summary.ok, summary.failed) == (1, 1)
    log = ingest.read().set_index("as_of_date")
    assert log.loc[D1, "status"] == "schema_drift"
    assert (storage.root / log.loc[D1, "raw_path"]).exists()  # evidence kept
    assert set(storage.read_table("toy")["v"]) == {2, 3}  # only D2's rows


def test_one_crashing_source_does_not_stop_the_others(env, tmp_path):
    storage, _ = env
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    ingest = IngestLog(storage.meta_dir, "multi")
    summaries = run_sources([CrashingSource(), ToySource()], [D2], RunContext(settings, D2),
                            http=FakeHttp({D2: b"4"}), storage=storage, ingest=ingest)
    assert summaries[0].failed == 1 and "boom" in summaries[0].error
    assert summaries[1].ok == 1


def test_batched_commits_write_everything(env):
    storage, run = env
    bodies = {D1 - timedelta(days=i): str(i).encode() for i in range(5)}
    summary, _, ingest = run(ToySource(), bodies, commit_every=2)
    assert summary.ok == 5
    assert len(storage.read_table("toy")) == 10
    assert len(ingest.read()) == 5


def test_write_raw_is_content_addressed(tmp_path):
    storage = Storage(tmp_path)
    req = FetchRequest("s", "r", D2, "u", key="SPY", ext="csv")
    p1, sha1 = storage.write_raw(req, b"hello")
    p2, _ = storage.write_raw(req, b"hello")
    p3, _ = storage.write_raw(req, b"changed")
    assert p1 == p2 and p1 != p3
    assert p1.name == f"2026-10-28_SPY__{sha1[:8]}.csv.gz"
    assert len(list(p1.parent.iterdir())) == 2


def test_upsert_partitions_and_idempotence(tmp_path):
    storage = Storage(tmp_path)
    df = conform(SPEC, pd.DataFrame({"trade_date": [D1, date(2026, 11, 2)], "sym": ["A", "A"],
                                     "v": [1, 2]}), datetime(2026, 11, 3, tzinfo=UTC))
    storage.upsert(SPEC, df)
    storage.upsert(SPEC, df)
    parts = sorted(p.parent.name for p in storage.table_dir("toy").glob("*/data.parquet"))
    assert parts == ["month=2026-10", "month=2026-11"]
    assert len(storage.read_table("toy")) == 2
    assert storage.read_latest("toy")["v"].tolist() == [2]


def test_conform_rejects_duplicate_primary_keys():
    df = pd.DataFrame({"trade_date": [D1, D1], "sym": ["A", "A"], "v": [1, 2]})
    with pytest.raises(SchemaDrift, match="share a primary key"):
        conform(SPEC, df)
    with pytest.raises(SchemaDrift, match="missing columns"):
        conform(SPEC, df.drop(columns="v"))
