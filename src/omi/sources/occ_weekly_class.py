"""OCC Weekly Volume Report by product class (ETF, equity = single stock, index).

All exchanges, one week per file, with what no daily file has: **opening vs closing** buys
and sells, **premium** dollars, and customer volume by **trade size** (1-10, 11-49, 50+
contracts). Each file has a standard-options block and a FLEX block, each with CALLS / PUTS
/ COMBINED, followed by premium by exchange.

Semantics, checked on the 2026-10-23 ETF file:
  * ``total_contracts`` = the row's buy-side contracts (open buys + close buys). For the
    TOTAL rows that equals contracts traded (total buys = total sells).
  * Market makers are reported entirely in the *close* columns (open = 0). OCC doesn't
    flag MM opening/closing, so open/close analysis should use customer and firm rows.

``reportDate`` must be the week's **Friday**, even when Friday is a market holiday (Good
Friday, July 4th). Any other date, or a week not yet published, returns
``Report is not available.`` History reached back to 2024-10-25 when checked on 2026-10-29.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

import pandas as pd

from omi.calendar import week_endings, yyyymmdd
from omi.sources.base import (
    FetchRequest,
    NotAvailable,
    RunContext,
    SchemaDrift,
    TableSpec,
    to_float,
    to_int,
)

NAME = "occ_weekly_class"
URL = ("https://marketdata.theocc.com/weekly-volume-reports?reportDate={d}&reportType=options"
       "&reportClass={cls}&format=csv")
CLASSES = ("etf", "equity", "index")
NOT_AVAILABLE = b"Report is not available"

HEADER_1 = ["", "", "Open Buys", "", "Close Buys", "", "Open Sells", "", "Close Sells", ""]
HEADER_2 = ["Trans Size", "Total Contracts", "Contracts", "Premiums", "Contracts", "Premiums",
            "Contracts", "Premiums", "Contracts", "Premiums"]
OPTION_TYPES = {"CALLS": "call", "PUTS": "put", "COMBINED": "combined"}
ACCOUNTS = {
    "CUST (1-10)": "cust_1_10", "CUST (11-49)": "cust_11_49", "CUST (50-UP)": "cust_50_up",
    "CUST (ALL)": "cust_all", "FIRM (ALL)": "firm", "M-M (ALL)": "mm",
    "TOTAL CALLS": "total", "TOTAL PUTS": "total", "TOTAL COMB": "total",
}
VALUE_COLS = ["open_buy_contracts", "open_buy_premium", "close_buy_contracts",
              "close_buy_premium", "open_sell_contracts", "open_sell_premium",
              "close_sell_contracts", "close_sell_premium"]

WEEKLY = TableSpec(
    name="occ_weekly_class",
    columns={
        "week_ending": "date",
        "product_class": "string",  # etf | equity (single stock) | index
        "flex": "bool",
        "option_type": "string",  # call | put | combined
        "account": "string",  # cust_1_10 | cust_11_49 | cust_50_up | cust_all | firm | mm | total
        "total_contracts": "int64",
        **{c: ("float64" if c.endswith("premium") else "int64") for c in VALUE_COLS},
    },
    pk=("week_ending", "product_class", "flex", "option_type", "account"),
    partition_col="week_ending",
)
EXCHANGE_PREMIUM = TableSpec(
    name="occ_weekly_class_exchange",
    columns={
        "week_ending": "date",
        "product_class": "string",
        "flex": "bool",
        "exchange": "string",
        "call_premium": "float64",
        "put_premium": "float64",
        "total_premium": "float64",
    },
    pk=("week_ending", "product_class", "flex", "exchange"),
    partition_col="week_ending",
)


class OccWeeklyClass:
    name = NAME
    tables = (WEEKLY, EXCHANGE_PREMIUM)

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        if not dates:
            return []
        cfg = ctx.settings.source(NAME).model_extra or {}
        classes = [c for c in cfg.get("classes", CLASSES) if c in CLASSES]
        return [
            FetchRequest(NAME, "weekly_volume", w, URL.format(d=yyyymmdd(w), cls=c), key=c,
                         ext="csv")
            for w in week_endings(min(dates), max(dates)) for c in classes
        ]

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        if raw.lstrip().startswith(NOT_AVAILABLE):
            raise NotAvailable(f"OCC: weekly {req.key} report for {req.as_of} not available")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8", errors="replace"))))
        title = rows[0][0] if rows and rows[0] else ""
        m = re.search(r"Week Ending (\d{2}/\d{2}/\d{4})", title)
        if not m:
            raise SchemaDrift(f"unexpected title {title!r}")
        week = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        if week != req.as_of:
            raise SchemaDrift(f"report is for week ending {week}, requested {req.as_of}")

        volume, premium = [], []
        flex, option_type, section = None, None, None
        for i, row in enumerate(rows[1:], start=2):
            cells = [c.strip() for c in row]
            first = cells[0] if cells else ""
            if not any(cells):
                section = None if section == "exchange" else section
                continue
            if len(cells) == 1 and first.endswith("Options"):
                flex, section = ("Flex" in first), None
            elif first == "Buys and Sells by Account Type":
                section = "volume"
            elif first == "Premiums by Exchange":
                section = "exchange"
            elif first == "Average Premium Per Contract":
                section = "average"  # derivable from the rows above; skipped
            elif section == "volume":
                if cells[: len(HEADER_1)] in (HEADER_1, HEADER_2):
                    continue
                if first in OPTION_TYPES:
                    option_type = OPTION_TYPES[first]
                    continue
                if first not in ACCOUNTS:
                    raise SchemaDrift(f"line {i}: unknown row label {first!r}")
                if len(cells) < 10 or option_type is None or flex is None:
                    raise SchemaDrift(f"line {i}: malformed account row {row}")
                volume.append({
                    "week_ending": week, "product_class": req.key, "flex": flex,
                    "option_type": option_type, "account": ACCOUNTS[first],
                    "total_contracts": to_int(cells[1]),
                    **{c: (to_float(v) if c.endswith("premium") else to_int(v))
                       for c, v in zip(VALUE_COLS, cells[2:10], strict=True)},
                })
            elif section == "exchange":
                if first == "Exchange":
                    if cells[:4] != ["Exchange", "Calls", "Puts", "Combined"]:
                        raise SchemaDrift(f"line {i}: premium header changed: {row}")
                elif first != "OCC TOTALS":
                    premium.append({
                        "week_ending": week, "product_class": req.key, "flex": flex,
                        "exchange": first, "call_premium": to_float(cells[1]),
                        "put_premium": to_float(cells[2]), "total_premium": to_float(cells[3]),
                    })

        if not volume:
            raise SchemaDrift("no account-type rows found")
        return {WEEKLY.name: pd.DataFrame(volume), EXCHANGE_PREMIUM.name: pd.DataFrame(premium)}
