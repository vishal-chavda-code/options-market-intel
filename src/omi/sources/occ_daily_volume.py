"""OCC Daily Volume Statistics: market-wide contracts per day by product class.

Each file is **month to date**: one row per session from the 1st through ``reportDate``,
then Total / Avg / YTD rows, then the same layout for futures. So one request per month
covers that month. ``Equity`` here includes ETF options; it counts contracts (not sides).
Days when NYSE was closed can still appear with index/futures volume only (2025-01-09, the
national day of mourning: equity 0, index 32,813); they are kept as published.
Two blocks:
    Daily OCC Contract Volume - <Month YYYY>:   Date,Equity,Index/Others,Debt,Futures,OCC Total
    Daily Futures Contract Volume -<Month YYYY>: Date,Equity,Index/Others,OOF,OCC Total
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

import pandas as pd

from omi.calendar import trading_days, yyyymmdd
from omi.sources.base import FetchRequest, NotAvailable, RunContext, SchemaDrift, TableSpec, to_int

NAME = "occ_daily_volume"
URL = "https://marketdata.theocc.com/daily-volume-statistics?reportDate={d}&format=csv"
OPTIONS_HEADER = ["Date", "Equity", "Index/Others", "Debt", "Futures", "OCC Total"]
FUTURES_HEADER = ["Date", "Equity", "Index/Others", "OOF", "OCC Total"]

DAILY = TableSpec(
    name="occ_daily_volume",
    columns={"trade_date": "date", "equity": "int64", "index_other": "int64", "debt": "int64",
             "futures": "int64", "occ_total": "int64"},
    pk=("trade_date",),
    partition_col="trade_date",
)
DAILY_FUTURES = TableSpec(
    name="occ_daily_futures_volume",
    columns={"trade_date": "date", "equity": "int64", "index_other": "int64", "oof": "int64",
             "occ_total": "int64"},
    pk=("trade_date",),
    partition_col="trade_date",
)


class OccDailyVolume:
    name = NAME
    tables = (DAILY, DAILY_FUTURES)

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        """One request per finished month (its last session covers the whole month), and one
        per session for a month still in progress, so a not-yet-published latest day can't
        hold back the days before it."""
        if not dates:
            return []
        end = max(dates)
        by_month: dict[tuple[int, int], list[date]] = defaultdict(list)
        for d in dates:
            by_month[(d.year, d.month)].append(d)
        wanted: list[date] = []
        for (year, month), days in sorted(by_month.items()):
            next_month = date(year + month // 12, month % 12 + 1, 1)
            month_last = trading_days(date(year, month, 1), next_month - timedelta(days=1))[-1]
            wanted += [month_last] if month_last <= end else sorted(days)
        return [FetchRequest(NAME, "daily_volume_statistics", d, URL.format(d=yyyymmdd(d)),
                             ext="csv") for d in wanted]

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8", errors="replace"))))
        blocks: dict[str, list[dict]] = {"options": [], "futures": []}
        block, header = None, None
        for row in rows:
            cells = [c.strip() for c in row]
            while cells and cells[-1] == "":
                cells.pop()  # every data row ends with a trailing comma
            if not cells:
                continue
            if cells[0].startswith("Daily OCC Contract Volume"):
                block, header = "options", OPTIONS_HEADER
            elif cells[0].startswith("Daily Futures Contract Volume"):
                block, header = "futures", FUTURES_HEADER
            elif cells[0] == "Date":
                if cells != header:
                    raise SchemaDrift(f"{block} header changed to {cells}")
            elif block and cells[0][:1].isdigit():
                values = dict(zip(header, cells, strict=True))
                values["Date"] = datetime.strptime(values["Date"], "%m/%d/%Y").date()
                blocks[block].append(values)
        if not blocks["options"]:
            raise NotAvailable(f"OCC: no daily rows in the {req.as_of:%Y-%m} file")
        # OCC serves the month through its latest published day, whatever date is asked for;
        # the request is satisfied once the file reaches the requested day.
        latest = max(r["Date"] for r in blocks["options"])
        if latest < req.as_of:
            raise NotAvailable(f"OCC: file ends {latest}, before {req.as_of}")

        options = pd.DataFrame([{
            "trade_date": r["Date"], "equity": to_int(r["Equity"]),
            "index_other": to_int(r["Index/Others"]), "debt": to_int(r["Debt"]),
            "futures": to_int(r["Futures"]), "occ_total": to_int(r["OCC Total"]),
        } for r in blocks["options"]])
        futures = pd.DataFrame([{
            "trade_date": r["Date"], "equity": to_int(r["Equity"]),
            "index_other": to_int(r["Index/Others"]), "oof": to_int(r["OOF"]),
            "occ_total": to_int(r["OCC Total"]),
        } for r in blocks["futures"]], columns=list(DAILY_FUTURES.columns))
        return {DAILY.name: options, DAILY_FUTURES.name: futures}
