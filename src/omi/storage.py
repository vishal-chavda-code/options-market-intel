"""On-disk layout under ``paths.data_dir``:

    raw/{source}/{report}/{YYYY}/{as_of}[_{key}]__{sha8}.{ext}.gz   write-once source of truth
    tables/{table}/{year=YYYY|month=YYYY-MM|day=YYYY-MM-DD}/data.parquet   tidy, deduped on PK
    meta/ingest_log/part-{run_id}-{seq}.parquet   one row per fetch attempt
    features/{name}.parquet   rebuilt by `omi features`

Raw files are gzipped (the OCC contract file is ~9 MB/day as text, ~1 MB gzipped); the
sha256 in the name and the log is of the uncompressed bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import secrets
import shutil
import time
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from omi.quality import conform
from omi.sources.base import FetchRequest, TableSpec

LOG_COLUMNS = {
    "run_id": "string",
    "source": "string",
    "report": "string",
    "as_of_date": "date",
    "key": "string",
    "url": "string",
    "http_status": "Int64",
    "bytes": "int64",
    "sha256": "string",
    "rows_parsed": "int64",
    # ok | not_available | http_error | network_error | parse_error | schema_drift | write_error
    "status": "string",
    "error_msg": "string",
    "raw_path": "string",
    "started_at": "timestamp",
    "finished_at": "timestamp",
}


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def _replace_with_retry(tmp: Path, path: Path) -> None:
    # Sync clients (OneDrive) and virus scanners can briefly lock files on Windows.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False, compression="zstd")
    _replace_with_retry(tmp, path)


class Storage:
    def __init__(self, data_dir: Path):
        self.root = data_dir
        self.raw_dir = data_dir / "raw"
        self.tables_dir = data_dir / "tables"
        self.meta_dir = data_dir / "meta"
        self.features_dir = data_dir / "features"

    # ---- raw layer -------------------------------------------------------------------
    def write_raw(self, req: FetchRequest, content: bytes) -> tuple[Path, str]:
        """Store the payload once; the same content for the same request is not rewritten."""
        sha = hashlib.sha256(content).hexdigest()
        folder = self.raw_dir / req.source / req.report / f"{req.as_of:%Y}"
        key = f"_{req.key}" if req.key else ""
        path = folder / f"{req.as_of:%Y-%m-%d}{key}__{sha[:8]}.{req.ext}.gz"
        if not path.exists():
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(gzip.compress(content, compresslevel=6))
            _replace_with_retry(tmp, path)
        return path, sha

    # ---- tables layer ----------------------------------------------------------------
    def table_dir(self, name: str) -> Path:
        return self.tables_dir / name

    def table_glob(self, name: str) -> str:
        """Glob for DuckDB's read_parquet over every partition of a table."""
        return (self.table_dir(name) / "*" / "data.parquet").as_posix()

    def has_table(self, name: str) -> bool:
        return any(self.table_dir(name).glob("*/data.parquet"))

    def upsert(self, spec: TableSpec, df: pd.DataFrame) -> int:
        """Merge conformed rows into their partitions; on a PK clash the newer row wins.

        Returns the number of rows written in this call. Idempotent: upserting the same rows
        twice leaves the table unchanged.
        """
        if df.empty:
            return 0
        parts = df[spec.partition_col].map(spec.partition_value)
        if spec.write_mode == "parts":
            for part, new_rows in df.groupby(parts, sort=True):
                folder = self.table_dir(spec.name) / str(part)
                name = f"part-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(4)}.parquet"
                write_parquet_atomic(new_rows.reset_index(drop=True), folder / name)
            return len(df)
        for part, new_rows in df.groupby(parts, sort=True):
            path = self.table_dir(spec.name) / str(part) / "data.parquet"
            if path.exists():
                old = conform(spec, pd.read_parquet(path))
                merged = pd.concat([old, new_rows], ignore_index=True)
            else:
                merged = new_rows
            merged = merged.sort_values("fetched_at", kind="stable")
            merged = merged.drop_duplicates(list(spec.pk), keep="last")
            merged = merged.sort_values(list(spec.pk), kind="stable").reset_index(drop=True)
            write_parquet_atomic(merged, path)
        return len(df)

    def compact(self, spec: TableSpec) -> int:
        """Fold pending part files into each partition's data.parquet (newest row per PK wins).

        Readers only ever see data.parquet, so a table written in "parts" mode is consistent
        once this has run. Safe to call at any time; returns the partitions compacted.
        """
        done = 0
        for folder in sorted(p for p in self.table_dir(spec.name).glob("*") if p.is_dir()):
            parts = sorted(folder.glob("part-*.parquet"))
            if not parts:
                continue
            files = [*parts]
            main = folder / "data.parquet"
            if main.exists():
                files.insert(0, main)
            file_list = ", ".join(f"'{f.as_posix()}'" for f in files)
            pk = ", ".join(spec.pk)
            tmp = folder / "data.parquet.tmp"
            with duckdb.connect() as con:
                con.execute(f"""
                    COPY (
                        SELECT * EXCLUDE (_rn) FROM (
                            SELECT *, row_number() OVER (
                                PARTITION BY {pk} ORDER BY fetched_at DESC) AS _rn
                            FROM read_parquet([{file_list}], union_by_name = true,
                                              hive_partitioning = false)
                        ) WHERE _rn = 1 ORDER BY {pk}
                    ) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
                """)
            _replace_with_retry(tmp, main)
            for p in parts:
                p.unlink(missing_ok=True)
            done += 1
        return done

    def drop_old_partitions(self, spec: TableSpec, keep: int) -> list[str]:
        """Delete all but the newest ``keep`` partitions of a table (rolling retention)."""
        folders = sorted(p for p in self.table_dir(spec.name).glob("*=*") if p.is_dir())
        dropped = folders[:-keep] if keep > 0 else folders
        for folder in dropped:
            shutil.rmtree(folder, ignore_errors=True)
        return [f.name for f in dropped]

    def prune_raw(self, source: str, before: date) -> int:
        """Delete a source's raw files whose as-of date (file name prefix) is before ``before``."""
        removed = 0
        for path in (self.raw_dir / source).rglob("*.gz"):
            try:
                as_of = date.fromisoformat(path.name[:10])
            except ValueError:
                continue
            if as_of < before:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def read_latest(self, name: str) -> pd.DataFrame:
        """Rows of the table's most recent partition (partition names sort chronologically)."""
        files = sorted(self.table_dir(name).glob("*/data.parquet"))
        return pd.read_parquet(files[-1]) if files else pd.DataFrame()

    def read_table(self, name: str, where: str | None = None) -> pd.DataFrame:
        """Whole table (optionally filtered with a SQL WHERE clause) as a DataFrame."""
        if not self.has_table(name):
            return pd.DataFrame()
        sql = (f"SELECT * FROM read_parquet('{self.table_glob(name)}', "
               "union_by_name = true, hive_partitioning = false)")
        if where:
            sql += f" WHERE {where}"
        with duckdb.connect() as con:
            return con.execute(sql).df()


class IngestLog:
    """Append-only log of fetch attempts, written as small Parquet parts per run."""

    def __init__(self, meta_dir: Path, run_id: str):
        self.dir = meta_dir / "ingest_log"
        self.run_id = run_id
        self._pending: list[dict] = []
        self._seq = 0

    def read(self) -> pd.DataFrame:
        files = sorted(self.dir.glob("part-*.parquet"))
        if not files:
            return pd.DataFrame(columns=list(LOG_COLUMNS))
        return pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)

    def ok_keys(self) -> set[tuple[str, str, date, str]]:
        """(source, report, as_of_date, key) of every request already stored successfully."""
        df = self.read()
        df = df[df["status"] == "ok"]
        return {
            (r.source, r.report, pd.Timestamp(r.as_of_date).date(), r.key or "")
            for r in df.itertuples(index=False)
        }

    def compact(self, max_parts: int = 200) -> int:
        """Merge the log's part files into one once there are more than ``max_parts``.

        The option-chain source logs ~4,000 fetches a night, so without this the log would
        grow by hundreds of small files per day and every run would slow down reading it.
        Returns the number of files merged (0 if below the threshold).
        """
        self.flush()
        files = sorted(self.dir.glob("part-*.parquet"))
        if len(files) <= max_parts:
            return 0
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        target = self.dir / f"part-{self.run_id}-compacted-{secrets.token_hex(2)}.parquet"
        write_parquet_atomic(df, target)
        for f in files:
            if f != target:
                f.unlink(missing_ok=True)
        return len(files)

    def add(self, row: dict) -> None:
        self._pending.append({"run_id": self.run_id, **row})

    def flush(self) -> None:
        if not self._pending:
            return
        df = pd.DataFrame(self._pending)
        for col, dtype in LOG_COLUMNS.items():
            if col not in df:
                df[col] = None
            if dtype == "date":
                df[col] = pd.to_datetime(df[col]).dt.date
            elif dtype == "timestamp":
                df[col] = pd.to_datetime(df[col], utc=True)
            else:
                df[col] = df[col].astype(dtype)
        self._seq += 1
        write_parquet_atomic(df[list(LOG_COLUMNS)],
                             self.dir / f"part-{self.run_id}-{self._seq:04d}.parquet")
        self._pending.clear()
