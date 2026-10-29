"""The source plugin contract.

A source is a pure planner and parser: ``plan`` says what to download, ``parse`` turns one
downloaded file into tidy DataFrames. The runner owns fetching, raw storage, logging,
validation and table writes, so sources can be unit-tested offline against fixtures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

import pandas as pd

if TYPE_CHECKING:
    from omi.config import Settings


class NotAvailable(Exception):
    """The source answered, but has no data for this date (yet). Retried on the next run."""


class ParseError(Exception):
    """The payload could not be parsed."""


class SchemaDrift(Exception):
    """The payload parsed, but its layout differs from what the parser was written against."""


@dataclass(frozen=True)
class FetchRequest:
    source: str
    report: str
    as_of: date  # the market date (or week-ending date) the data describes
    url: str
    key: str = ""  # extra identity for per-symbol / per-class reports, e.g. "SPY" or "etf"
    ext: str = "txt"  # raw file extension

    @property
    def log_key(self) -> tuple[str, str, date, str]:
        return (self.source, self.report, self.as_of, self.key)


@dataclass(frozen=True)
class TableSpec:
    """A stored table: columns with pandas dtypes, primary key, and partitioning.

    Every table also gets a ``fetched_at`` (UTC) column; on a primary-key clash the row with
    the later ``fetched_at`` wins.
    """

    name: str
    columns: dict[str, str]
    pk: tuple[str, ...]
    partition_col: str  # a date column
    partition: str = "year"  # year | month | day
    # "merge": every commit rewrites the partition file (fine for small tables).
    # "parts": commits append part files and the runner compacts them into data.parquet once
    # the source finishes (for big tables such as option chains, where rewriting a 100 MB
    # partition after every batch would be quadratic).
    write_mode: str = "merge"

    def partition_value(self, d: date) -> str:
        if self.partition == "day":
            return f"day={d:%Y-%m-%d}"
        if self.partition == "month":
            return f"month={d:%Y-%m}"
        return f"year={d:%Y}"


def _empty(name: str) -> pd.DataFrame:
    return pd.DataFrame()


@dataclass
class RunContext:
    """What a source may look at while planning: settings, the run date, and stored tables."""

    settings: Settings
    today: date  # calendar date (ET) of the run
    # Rows of a table's most recent partition (e.g. the latest day of contract volume).
    read_latest: Callable[[str], pd.DataFrame] = field(default=_empty)
    started_at: datetime | None = None


class Source(Protocol):
    name: str
    tables: tuple[TableSpec, ...]

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        """Requests needed to cover ``dates`` (NYSE sessions)."""
        ...

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        """Table name -> rows. Raise NotAvailable / SchemaDrift / ParseError as appropriate."""
        ...


def to_int(text: str) -> int:
    """'1,234' or '"1,234"' -> 1234."""
    return int(text.strip().strip('"').replace(",", ""))


def to_float(text: str) -> float:
    """'$1,234.50' -> 1234.5."""
    return float(text.strip().strip('"').replace("$", "").replace(",", ""))
