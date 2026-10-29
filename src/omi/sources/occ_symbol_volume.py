"""OCC Volume Query: per-underlying volume split by account type, call/put and exchange.

One request per underlying per day, so it covers a chosen set: ``universe.core`` +
``universe.extra`` + the ``top_n`` most active underlyings in the latest stored
``occ_contract_volume`` day. OCC serves about two years of history (2024-11-01 still worked
on 2026-10-29).

Volumes count trade sides, like the contract-volume file. We omit ``contractDt``, which
filters to a single expiry date (verified: SPY on 2026-10-28 summed to 15.9M sides
without it, and 10.7M with ``contractDt=20261028``, i.e. 0DTE only).

CSV header ``quantity,underlying,symbol,actype,porc,exchange,actdate``; data rows carry one
extra trailing empty field. ``actype``: C customer, F firm, M market maker.
``porc``: C call, P put.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

import pandas as pd

from omi.calendar import yyyymmdd
from omi.sources.base import FetchRequest, NotAvailable, RunContext, SchemaDrift, TableSpec

NAME = "occ_symbol_volume"
URL = ("https://marketdata.theocc.com/volume-query?reportDate={d}&format=csv&volumeQueryType=O"
       "&symbolType=U&symbol={sym}&reportType=D&accountType=ALL&productKind=ALL&porc=BOTH")
HEADER = ["quantity", "underlying", "symbol", "actype", "porc", "exchange", "actdate"]

SYMBOL_VOLUME = TableSpec(
    name="occ_symbol_volume",
    columns={
        "trade_date": "date",
        "underlying": "string",
        "symbol": "string",
        "account_type": "string",  # C customer, F firm, M market maker
        "cp": "string",  # C call, P put
        "exchange": "string",  # exchange name, e.g. CBOE
        "sides": "int64",
    },
    pk=("trade_date", "underlying", "symbol", "account_type", "cp", "exchange"),
    partition_col="trade_date",
    partition="month",
)


def top_underlyings(contract_volume: pd.DataFrame, n: int) -> list[str]:
    """The ``n`` underlyings with the most option sides on the latest stored day."""
    if contract_volume.empty or n <= 0:
        return []
    df = contract_volume[contract_volume["product_kind"].isin(["OSTK", "OIND"])]
    latest = df[df["trade_date"] == df["trade_date"].max()]
    sides = latest["cust_sides"] + latest["firm_sides"] + latest["mm_sides"]
    return list(sides.groupby(latest["underlying"]).sum().nlargest(n).index)


class OccSymbolVolume:
    name = NAME
    tables = (SYMBOL_VOLUME,)

    def symbols(self, ctx: RunContext) -> list[str]:
        cfg = ctx.settings.source(NAME).model_extra or {}
        top_n = int(cfg.get("top_n", 0))
        universe = [*ctx.settings.universe.core, *ctx.settings.universe.extra]
        top = top_underlyings(ctx.read_latest("occ_contract_volume"), top_n) if top_n else []
        return list(dict.fromkeys(s.lstrip("_").upper() for s in [*universe, *top]))

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        syms = self.symbols(ctx)
        return [
            FetchRequest(NAME, "volume_query", d, URL.format(d=yyyymmdd(d), sym=s), key=s,
                         ext="csv")
            for d in dates for s in syms
        ]

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        text = raw.decode("utf-8", errors="replace")
        rows = [r for r in csv.reader(io.StringIO(text)) if r]
        if not rows or rows[0][: len(HEADER)] != HEADER:
            first = rows[0] if rows else "<empty>"
            if rows and len(rows[0]) == 1:  # a plain-text message such as "Symbol is required."
                raise NotAvailable(f"OCC: {first[0]}")
            raise SchemaDrift(f"unexpected header {first}")
        data = [r[: len(HEADER)] for r in rows[1:]]
        if not data:
            raise NotAvailable(f"OCC: no volume rows for {req.key} on {req.as_of}")
        df = pd.DataFrame(data, columns=HEADER)
        returned = {datetime.strptime(x, "%m/%d/%Y").date() for x in df["actdate"]}
        if returned != {req.as_of}:
            raise NotAvailable(f"OCC returned dates {sorted(returned)} for {req.as_of}")
        out = pd.DataFrame({
            "trade_date": req.as_of,
            "underlying": df["underlying"].str.strip(),
            "symbol": df["symbol"].str.strip(),
            "account_type": df["actype"].str.strip(),
            "cp": df["porc"].str.strip(),
            "exchange": df["exchange"].str.strip(),
            "sides": df["quantity"].astype("int64"),
        })
        return {SYMBOL_VOLUME.name: out}
