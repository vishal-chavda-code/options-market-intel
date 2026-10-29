"""OCC Contract Volume Download: the most granular free all-exchange options volume.

One row per underlying x option root x exchange x expiry, split into customer, firm and
market-maker volume. No call/put split and no strike (for those see ``occ_symbol_volume``).

Volumes count **trade sides**: every contract has a buyer and a seller, so
customer + firm + market maker = 2 x contracts traded. Verified 2026-10-28: half of the
OSTK sides equals OCC's published Equity volume (50,854,822) exactly, and half of the
OIND sides equals Index/Others (5,025,345).

OCC keeps only ~30 sessions of this file (earliest available on 2026-10-29 was 2026-09-17),
so the daily archive is the only way to build history.

Fixed-width layout, 52 chars per line (decoded from real files, see tests/fixtures/occ/):
    [0:6]   underlying        [6:12]  option root (SPXW, 2SPY...)   [12]  exchange code
    [13:22] customer volume   [22:31] firm volume                   [31:40] market-maker volume
    [40:44] product kind: OSTK stock/ETF options, OIND index options, OCHF futures,
            OOOF options on futures
    [44:52] expiry YYYYMMDD
First line: ``H`` + report date MMDDYYYY (twice). Unpublished dates return HTTP 200 with
the body ``File requested does not exist.``
"""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from omi.calendar import yyyymmdd
from omi.sources.base import FetchRequest, NotAvailable, RunContext, SchemaDrift, TableSpec

NAME = "occ_contract_volume"
URL = "https://marketdata.theocc.com/cont-volume-download?reportDate={d}&format=txt"
LINE_LEN = 52
NOT_AVAILABLE = b"File requested does not exist"

CONTRACT_VOLUME = TableSpec(
    name="occ_contract_volume",
    columns={
        "trade_date": "date",
        "underlying": "string",
        "symbol": "string",
        "exchange": "string",  # OCC one-letter code; names in config/reference/occ_exchanges.csv
        "product_kind": "string",
        "expiry": "date",
        "cust_sides": "int64",
        "firm_sides": "int64",
        "mm_sides": "int64",
    },
    pk=("trade_date", "underlying", "symbol", "exchange", "product_kind", "expiry"),
    partition_col="trade_date",
    partition="day",
)


class OccContractVolume:
    name = NAME
    tables = (CONTRACT_VOLUME,)

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        return [FetchRequest(NAME, "contract_volume", d, URL.format(d=yyyymmdd(d)), ext="txt")
                for d in dates]

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        if raw.lstrip().startswith(NOT_AVAILABLE):
            raise NotAvailable(f"OCC: file for {req.as_of} not published (yet)")
        lines = raw.decode("latin-1").splitlines()
        header = lines[0].split()
        if not header or not header[0].startswith("H") or len(header[0]) != 9:
            raise SchemaDrift(f"unexpected header line {lines[0]!r}")
        file_date = datetime.strptime(header[0][1:], "%m%d%Y").date()
        if file_date != req.as_of:
            raise SchemaDrift(f"file is for {file_date}, requested {req.as_of}")

        body = pd.Series([ln for ln in lines[1:] if ln.strip()], dtype="string")
        bad = body.str.len() != LINE_LEN
        if bad.any():
            first = int(bad.idxmax()) + 2
            raise SchemaDrift(
                f"{int(bad.sum())} lines are not {LINE_LEN} chars (first: line {first})"
            )
        df = pd.DataFrame({
            "trade_date": req.as_of,
            "underlying": body.str.slice(0, 6).str.strip(),
            "symbol": body.str.slice(6, 12).str.strip(),
            "exchange": body.str.slice(12, 13),
            "cust_sides": body.str.slice(13, 22),
            "firm_sides": body.str.slice(22, 31),
            "mm_sides": body.str.slice(31, 40),
            "product_kind": body.str.slice(40, 44),
            "expiry": pd.to_datetime(body.str.slice(44, 52), format="%Y%m%d"),
        })
        for col in ("cust_sides", "firm_sides", "mm_sides"):
            if not df[col].str.fullmatch(r"\d{9}").all():
                raise SchemaDrift(f"non-numeric values in {col}")
            df[col] = df[col].astype("int64")
        return {CONTRACT_VOLUME.name: df}
