"""Runs sources: plan -> skip what is already stored -> fetch -> raw -> parse -> conform -> upsert.

Each source runs in isolation; an exception in one is logged and the next one still runs.
Table rows and ingest-log rows are committed together every ``pipeline.commit_every``
fetches, data first, so a crash can never leave a log row saying "ok" for data that
was not written.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

import pandas as pd

from omi.http import FetchResult
from omi.quality import conform
from omi.sources.base import (
    FetchRequest,
    NotAvailable,
    ParseError,
    RunContext,
    SchemaDrift,
    Source,
)
from omi.storage import IngestLog, Storage

log = logging.getLogger(__name__)


class Fetcher(Protocol):
    """The slice of HttpClient the runner needs (lets tests pass a fake)."""

    def get(self, url: str) -> FetchResult: ...


@dataclass
class SourceSummary:
    source: str
    planned: int = 0
    skipped: int = 0
    ok: int = 0
    not_available: int = 0
    failed: int = 0
    rows: int = 0
    error: str = ""

    def line(self) -> str:
        text = (f"{self.source}: planned {self.planned}, skipped {self.skipped} (already stored), "
                f"ok {self.ok}, not available {self.not_available}, failed {self.failed}, "
                f"rows {self.rows:,}")
        return text + (f" -- {self.error}" if self.error else "")


def _now() -> datetime:
    return datetime.now(UTC)


class _Batch:
    def __init__(self, source: Source, storage: Storage, ingest: IngestLog):
        self.source, self.storage, self.ingest = source, storage, ingest
        self.frames: dict[str, list[pd.DataFrame]] = defaultdict(list)
        self.rows: list[dict] = []

    def commit(self) -> None:
        if not self.rows:
            return
        specs = {t.name: t for t in self.source.tables}
        try:
            for name, frames in self.frames.items():
                self.storage.upsert(specs[name], pd.concat(frames, ignore_index=True))
        except Exception as exc:  # noqa: BLE001 - recorded in the log, then surfaced
            for row in self.rows:
                if row["status"] == "ok":
                    row["status"], row["error_msg"] = "write_error", f"{type(exc).__name__}: {exc}"
            self._flush_log()
            raise
        self._flush_log()

    def _flush_log(self) -> None:
        for row in self.rows:
            self.ingest.add(row)
        self.ingest.flush()
        self.frames.clear()
        self.rows.clear()


def run_source(
    source: Source,
    dates: list[date],
    ctx: RunContext,
    *,
    http: Fetcher,
    storage: Storage,
    ingest: IngestLog,
    force: bool = False,
    commit_every: int = 20,
) -> SourceSummary:
    summary = SourceSummary(source.name)
    requests_: list[FetchRequest] = source.plan(dates, ctx)
    summary.planned = len(requests_)
    done = set() if force else ingest.ok_keys()
    specs = {t.name: t for t in source.tables}
    batch = _Batch(source, storage, ingest)

    for req in requests_:
        if req.log_key in done:
            summary.skipped += 1
            continue
        started = _now()
        res = http.get(req.url)
        row = {
            "source": req.source, "report": req.report, "as_of_date": req.as_of, "key": req.key,
            "url": res.url, "http_status": res.status, "bytes": len(res.content), "sha256": None,
            "rows_parsed": 0, "status": "ok", "error_msg": None, "raw_path": None,
            "started_at": started,
        }
        if not res.ok:
            row["status"] = "http_error" if res.status is not None else "network_error"
            row["error_msg"] = f"[{res.error_kind}] {res.error}"
        else:
            try:
                parsed = source.parse(req, res.content)
                checked = {name: conform(specs[name], df, res.fetched_at)
                           for name, df in parsed.items()}
            except NotAvailable as exc:
                row["status"], row["error_msg"] = "not_available", str(exc)
            except SchemaDrift as exc:
                row["status"], row["error_msg"] = "schema_drift", str(exc)
            except (ParseError, ValueError, KeyError, IndexError) as exc:
                row["status"], row["error_msg"] = "parse_error", f"{type(exc).__name__}: {exc}"
            else:
                for name, df in checked.items():
                    batch.frames[name].append(df)
                row["rows_parsed"] = sum(len(df) for df in checked.values())
            # Keep the payload as evidence unless the source simply had nothing for us.
            if row["status"] != "not_available":
                path, sha = storage.write_raw(req, res.content)
                row["raw_path"], row["sha256"] = str(path.relative_to(storage.root)), sha

        row["finished_at"] = _now()
        status = row["status"]
        if status == "ok":
            summary.ok += 1
            summary.rows += row["rows_parsed"]
        elif status == "not_available":
            summary.not_available += 1
        else:
            summary.failed += 1
            log.warning("%s %s %s %s: %s", req.source, req.report, req.as_of, req.key or "",
                        row["error_msg"])
        batch.rows.append(row)
        if len(batch.rows) >= commit_every:
            batch.commit()
    batch.commit()
    return summary


def run_sources(
    sources: list[Source],
    dates: list[date],
    ctx: RunContext,
    *,
    http: Fetcher,
    storage: Storage,
    ingest: IngestLog,
    force: bool = False,
    commit_every: int = 20,
) -> list[SourceSummary]:
    """Run each source in turn. One source failing never stops the others."""
    summaries = []
    for source in sources:
        try:
            summary = run_source(source, dates, ctx, http=http, storage=storage, ingest=ingest,
                                 force=force, commit_every=commit_every)
        except Exception as exc:  # noqa: BLE001 - isolate sources from each other
            log.exception("source %s crashed", source.name)
            summary = SourceSummary(source.name, failed=1, error=f"{type(exc).__name__}: {exc}")
        log.info(summary.line())
        summaries.append(summary)
    return summaries
