"""Nasdaq Trader symbol directory: every US-listed symbol with an ETF Y/N flag.

This is what separates ETF options from single-stock options: OCC files both under product
kind ``OSTK``. The files describe *current* listings only, so we snapshot them daily
(``as_of`` = run date) and classify history against the latest snapshot. ETF status rarely
changes, so this is a reasonable approximation for backfilled dates.

Two files: ``nasdaqlisted.txt`` (Nasdaq-listed) and ``otherlisted.txt`` (NYSE, NYSE American,
NYSE Arca, Cboe BZX, IEX...). Pipe-delimited, header row, trailing
``File Creation Time: ...`` row.
"""

from __future__ import annotations

import io
import re
from datetime import date

import pandas as pd

from omi.sources.base import FetchRequest, RunContext, SchemaDrift, TableSpec

NAME = "symbol_directory"
BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"

# file -> (symbol column, exchange column or None, expected header)
FILES = {
    "nasdaqlisted": ("Symbol", None, [
        "Symbol", "Security Name", "Market Category", "Test Issue", "Financial Status",
        "Round Lot Size", "ETF", "NextShares"]),
    "otherlisted": ("NASDAQ Symbol", "Exchange", [
        "ACT Symbol", "Security Name", "Exchange", "CQS Symbol", "ETF", "Round Lot Size",
        "Test Issue", "NASDAQ Symbol"]),
}

LISTED = TableSpec(
    name="ref_listed_symbols",
    columns={
        "as_of": "date",
        "listing_file": "string",
        "symbol": "string",  # as published, e.g. "BRK.B"
        "occ_symbol": "string",  # letters/digits only, e.g. "BRKB": how OCC writes underlyings
        "name": "string",
        "listing_exchange": "string",  # Q = Nasdaq; N, A, P, Z, V per otherlisted.txt
        "is_etf": "bool",
        "is_test_issue": "bool",
    },
    pk=("as_of", "listing_file", "symbol"),
    partition_col="as_of",
)


def occ_symbol(symbol: str) -> str:
    """Normalise a listing symbol to OCC's underlying style (drop '.', '$', '/', spaces)."""
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


class SymbolDirectory:
    name = NAME
    tables = (LISTED,)

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        # Current listings only: one snapshot per run day, whatever range was asked for.
        return [FetchRequest(NAME, "listings", ctx.today, f"{BASE}{f}.txt", key=f, ext="txt")
                for f in FILES]

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        sym_col, exch_col, header = FILES[req.key]
        text = raw.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if lines[0].split("|") != header:
            raise SchemaDrift(f"{req.key}: header changed to {lines[0]!r}")
        body = [ln for ln in lines[1:] if not ln.startswith("File Creation Time")]
        df = pd.read_csv(io.StringIO("\n".join([lines[0], *body])), sep="|", dtype=str,
                         keep_default_na=False)
        out = pd.DataFrame({
            "as_of": req.as_of,
            "listing_file": req.key,
            "symbol": df[sym_col].str.strip(),
            "name": df["Security Name"].str.strip(),
            "listing_exchange": df[exch_col].str.strip() if exch_col else "Q",
            "is_etf": df["ETF"].str.strip().eq("Y"),
            "is_test_issue": df["Test Issue"].str.strip().eq("Y"),
        })
        out["occ_symbol"] = out["symbol"].map(occ_symbol)
        out = out[out["symbol"] != ""]
        return {LISTED.name: out}
