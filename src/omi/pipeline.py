"""High-level jobs behind the CLI: backfill, daily, health."""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from omi import calendar as cal
from omi.config import Settings
from omi.features import chains, product_class
from omi.http import HttpClient
from omi.quality import conform
from omi.runner import Fetcher, SourceSummary, run_source
from omi.sources import REGISTRY
from omi.sources.base import RunContext, Source
from omi.storage import IngestLog, Storage, new_run_id, write_parquet_atomic

log = logging.getLogger(__name__)


class PipelineError(Exception):
    """A job was asked for something it cannot do (unknown source, no start date...)."""


def ingestable(settings: Settings, names: list[str] | None = None) -> list[Source]:
    """Sources to run, in registry order: the named ones, else every enabled one."""
    if names:
        unknown = sorted(set(names) - set(REGISTRY))
        if unknown:
            raise PipelineError(f"not an ingestable source: {', '.join(unknown)} "
                                f"(choose from {', '.join(REGISTRY)})")
        return [s for n, s in REGISTRY.items() if n in names]
    return [s for n, s in REGISTRY.items() if settings.source(n).enabled]


def _context(settings: Settings, storage: Storage) -> RunContext:
    now = cal.now_et()
    return RunContext(settings=settings, today=now.date(), read_latest=storage.read_latest,
                      started_at=now)


def _setup(settings: Settings, http: Fetcher | None):
    storage = Storage(settings.paths.data_dir)
    ingest = IngestLog(storage.meta_dir, new_run_id())
    return storage, ingest, http or HttpClient(settings.network)


def backfill(
    settings: Settings,
    names: list[str] | None,
    start: date | None,
    end: date | None,
    *,
    force: bool = False,
    http: Fetcher | None = None,
) -> list[SourceSummary]:
    """Fetch every session in [start, end] that is not stored yet, source by source."""
    storage, ingest, client = _setup(settings, http)
    ctx = _context(settings, storage)
    end = end or cal.last_completed_session()
    summaries = []
    for source in ingestable(settings, names):
        cfg_start = (settings.source(source.name).model_extra or {}).get("backfill_start")
        first = start or (date.fromisoformat(cfg_start) if cfg_start else end)
        dates = cal.trading_days(first, end)
        log.info("backfill %s: %d sessions %s..%s", source.name, len(dates), first, end)
        try:
            summary = run_source(source, dates, ctx, http=client, storage=storage,
                                 ingest=ingest, force=force,
                                 commit_every=settings.pipeline.commit_every)
        except Exception as exc:  # noqa: BLE001 - one source must not stop the others
            log.exception("source %s crashed", source.name)
            summary = SourceSummary(source.name, failed=1, error=f"{type(exc).__name__}: {exc}")
        log.info(summary.line())
        summaries.append(summary)
    return summaries


def daily(
    settings: Settings,
    as_of: date | None = None,
    *,
    force: bool = False,
    http: Fetcher | None = None,
) -> list[SourceSummary]:
    """The nightly job: the last completed session plus a catch-up window, then features."""
    end = as_of or cal.last_completed_session()
    start = end
    for _ in range(settings.pipeline.daily_catchup_sessions):
        start = cal.previous_session(start)
    summaries = backfill(settings, None, start, end, force=force, http=http)
    storage = Storage(settings.paths.data_dir)
    counts = build_features(storage, settings)
    log.info("features rebuilt: %s", counts)
    merged = IngestLog(storage.meta_dir, new_run_id()).compact()
    if merged:
        log.info("ingest log: merged %d part files", merged)
    return summaries


def build_features(storage: Storage, settings: Settings) -> dict[str, int]:
    """Every feature family, in dependency order (chain features use the product classes)."""
    counts = product_class.build(storage, settings)
    try:
        counts.update(chains.build(storage, settings))
    except Exception:  # noqa: BLE001 - chain features must never block the OCC features
        log.exception("chain features failed")
    return counts


def import_seed(settings: Settings, seed_dir: Path) -> dict[str, int]:
    """Merge a committed snapshot (``seed/``) into ``data/``.

    Tables are upserted by primary key, so this is safe to repeat and safe after the pipeline
    has already run; on a clash the row fetched later (usually the local one) wins. Raw files
    and ingest-log parts are copied only when missing, so already-stored dates are skipped by
    later backfills.
    """
    storage = Storage(settings.paths.data_dir)
    specs = {t.name: t for s in REGISTRY.values() for t in s.tables}
    counts: dict[str, int] = {}
    tables_dir = seed_dir / "tables"
    for table_dir in sorted(p for p in tables_dir.iterdir() if p.is_dir()):
        spec = specs.get(table_dir.name)
        if spec is None:
            log.warning("seed: skipping unknown table %s", table_dir.name)
            continue
        counts[table_dir.name] = sum(
            storage.upsert(spec, conform(spec, pd.read_parquet(f)))
            for f in sorted(table_dir.glob("*/data.parquet"))
        )
    copied = 0
    for sub in ("raw", "meta/ingest_log"):
        src_root = seed_dir / sub
        for src in sorted(src_root.rglob("*")):
            dest = storage.root / sub / src.relative_to(src_root)
            if src.is_file() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied += 1
    counts["files_copied"] = copied
    return counts


def health(settings: Settings, now: datetime | None = None, *, write: bool = True) -> pd.DataFrame:
    """Per (source, report): last stored date, last attempt and its status, recent errors.

    Also written to data/meta/health.parquet unless ``write=False`` (the dashboard is
    read-only and computes it in memory).
    """
    storage = Storage(settings.paths.data_dir)
    df = IngestLog(storage.meta_dir, "health").read()
    if df.empty:
        return pd.DataFrame()
    df = df.sort_values("finished_at")
    latest_session = cal.last_completed_session(now)
    rows = []
    for (source, report), g in df.groupby(["source", "report"], sort=True):
        ok = g[g["status"] == "ok"]
        last_ok = pd.Timestamp(ok["as_of_date"].max()).date() if not ok.empty else None
        errors = g[~g["status"].isin(["ok", "not_available"])]
        behind = (len(cal.trading_days(last_ok, latest_session)) - 1) if last_ok else None
        rows.append({
            "source": source,
            "report": report,
            "last_ok_as_of": last_ok,
            "sessions_behind": behind,
            "last_attempt_at": g["finished_at"].iloc[-1],
            "last_status": g["status"].iloc[-1],
            "ok_fetches": len(ok),
            "not_available": int((g["status"] == "not_available").sum()),
            "errors": len(errors),
            "last_error": errors["error_msg"].iloc[-1] if not errors.empty else None,
        })
    out = pd.DataFrame(rows)
    if write:
        write_parquet_atomic(out, Path(storage.meta_dir) / "health.parquet")
    return out
