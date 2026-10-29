"""Cboe U.S. Options daily market statistics (Cboe Options exchange only, not all exchanges).

Gives what OCC's daily files lack at the category level: **open interest** and put/call
ratios for Index, Exchange Traded Products and Equity options, plus individual index
products (SPX + SPXW, VIX, OEX, DJX, MRUT...).

The page is server-rendered HTML (Next.js). Table 1 is ``Ratios | Value``. Each other table
has a header row with the category name, then ``Name | Call | Put | Total``, then VOLUME
and OPEN INTEREST rows. A date with no data (not yet published, or a holiday) renders
with no tables at all. The page does not print the date it shows (only the requested
``dt`` is echoed), so a stale page cannot be detected from the HTML.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from bs4 import BeautifulSoup

from omi.sources.base import FetchRequest, NotAvailable, RunContext, SchemaDrift, TableSpec, to_int

NAME = "cboe_daily"
URL = "https://www.cboe.com/markets/us/options/market-statistics/daily?dt={d}"

VOLUME_OI = TableSpec(
    name="cboe_daily_volume",
    columns={"trade_date": "date", "category": "string", "call_vol": "int64", "put_vol": "int64",
             "total_vol": "int64", "call_oi": "int64", "put_oi": "int64", "total_oi": "int64"},
    pk=("trade_date", "category"),
    partition_col="trade_date",
)
PC_RATIOS = TableSpec(
    name="cboe_pc_ratios",
    columns={"trade_date": "date", "ratio_name": "string", "value": "float64"},
    pk=("trade_date", "ratio_name"),
    partition_col="trade_date",
)


def _rows(table) -> list[list[str]]:
    return [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")]


class CboeDailyStats:
    name = NAME
    tables = (VOLUME_OI, PC_RATIOS)

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        return [FetchRequest(NAME, "daily_market_statistics", d, URL.format(d=d.isoformat()),
                             ext="html") for d in dates]

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        soup = BeautifulSoup(raw, "lxml")
        tables = soup.find_all("table")
        if not tables:
            raise NotAvailable(f"Cboe: no statistics tables for {req.as_of}")

        ratio_rows = _rows(tables[0])
        if ratio_rows[0] != ["Ratios", "Value"]:
            raise SchemaDrift(f"first table header is {ratio_rows[0]}, expected Ratios | Value")
        ratios = pd.DataFrame(
            [{"trade_date": req.as_of, "ratio_name": name, "value": float(value)}
             for name, value in ratio_rows[1:]]
        )

        volume = []
        for table in tables[1:]:
            rows = _rows(table)
            if len(rows) < 4 or rows[1] != ["Name", "Call", "Put", "Total"]:
                raise SchemaDrift(f"unexpected category table layout: {rows[:2]}")
            category = rows[0][0]
            by_name = {r[0]: r[1:4] for r in rows[2:]}
            if set(by_name) != {"VOLUME", "OPEN INTEREST"}:
                raise SchemaDrift(f"{category}: rows are {sorted(by_name)}")
            vol, oi = by_name["VOLUME"], by_name["OPEN INTEREST"]
            volume.append({
                "trade_date": req.as_of, "category": category,
                "call_vol": to_int(vol[0]), "put_vol": to_int(vol[1]), "total_vol": to_int(vol[2]),
                "call_oi": to_int(oi[0]), "put_oi": to_int(oi[1]), "total_oi": to_int(oi[2]),
            })
        return {VOLUME_OI.name: pd.DataFrame(volume), PC_RATIOS.name: ratios}
