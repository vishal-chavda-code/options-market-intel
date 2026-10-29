"""Cboe delayed option chains: every listed contract's open interest, volume, quote and greeks.

Endpoint (undocumented; it backs the quote pages on cboe.com, see PROJECT_SPEC.md 5.4):
``https://cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json``. Index symbols take
an underscore prefix (``_SPX``, ``_VIX``); stocks and ETFs are plain (``AAPL``, ``SPY``).
``cdn.cboe.com`` redirects to ``cdn-api.cboe.com``.

**It is a snapshot of "now", not a dated report.** There is no history to backfill, so the
planner only plans the last completed session, and only while the clock is inside the
snapshot window (default: after 16:15 ET on that session's own calendar day). That keeps
every stored day comparable: quotes are the close, and ``open_interest`` is OCC's
start-of-day figure for that session (OCC recomputes OI overnight, so a pre-open snapshot
would already carry the next day's OI). The morning retry run therefore skips this source.

Symbols (``scope`` in config):
  * ``all``      every stock/ETF/index underlying that traded options on the latest stored
                 ``occ_contract_volume`` day, plus anything snapshotted earlier this month
                 (so a name doesn't drop out on a quiet day), plus ``universe``;
  * ``top_n``    the ``top_n`` most active underlyings plus ``universe``;
  * ``universe`` only ``universe.core`` + ``universe.extra``.
Ordered universe first, then by OCC volume, so an interrupted run has the important names.

Storage: ``chain_eod`` is partitioned by day and written in "parts" mode (compacted after the
run). Only the newest ``retention_sessions`` days are kept (default 45); raw JSON is kept
for ``keep_raw_sessions`` days (default 3) because it is ~10x the Parquet size.

Payload fields used (verified on ``_VIX`` 2026-10-29, fixture
``tests/fixtures/cboe_chains/_VIX_trimmed.json``): top-level ``timestamp``, ``data.symbol``,
``data.security_type``, ``data.current_price``, ``data.prev_day_close``, ``data.iv30``, and
per contract ``option`` (OSI: root + YYMMDD + C/P + strike x 1000), ``bid``, ``ask``,
``last_trade_price``, ``prev_day_close``, ``volume``, ``open_interest``, ``iv``, ``delta``,
``gamma``, ``vega``, ``theta``, ``theo``, ``last_trade_time``. ``iv == 0`` means "not
computed" (seen on a deep ITM call) and is stored as missing.
"""

from __future__ import annotations

import json
import logging
from datetime import date, time

import numpy as np
import pandas as pd

from omi import calendar as cal
from omi.sources.base import (
    FetchRequest,
    NotAvailable,
    ParseError,
    RunContext,
    SchemaDrift,
    TableSpec,
)

log = logging.getLogger(__name__)

NAME = "cboe_chains"
URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"

CHAIN = TableSpec(
    name="chain_eod",
    columns={
        "as_of": "date",  # the session the snapshot was taken after
        "underlying": "string",  # OCC-style underlying symbol (letters/digits), e.g. SPX, BRKB
        "option": "string",  # OSI symbol as Cboe sends it, e.g. SPXW261118C05800000
        "root": "string",  # option root, e.g. SPXW
        "expiry": "date",
        "cp": "string",  # C / P
        "strike": "float64",
        "bid": "float64",
        "ask": "float64",
        "last": "float64",  # last trade price (may be days old; see last_trade_at)
        "prev_close": "float64",
        "volume": "int64",  # contracts traded in the session
        "open_interest": "int64",  # OCC open interest at the start of the session
        "iv": "float64",  # implied vol as a decimal (Cboe's calc); NaN when Cboe sends 0
        "delta": "float64",
        "gamma": "float64",
        "vega": "float64",
        "theta": "float64",
        "theo": "float64",  # Cboe theoretical value
        "last_trade_at": "timestamp",  # UTC; null if never traded
    },
    pk=("as_of", "underlying", "option"),
    partition_col="as_of",
    partition="day",
    write_mode="parts",
)

UNDERLYING = TableSpec(
    name="chain_underlying",
    columns={
        "as_of": "date",
        "underlying": "string",
        "cboe_symbol": "string",  # as Cboe labels it, e.g. ^SPX
        "security_type": "string",  # index / stock / etf ... as Cboe sends it
        "price": "float64",  # underlying last (delayed)
        "prev_close": "float64",
        "iv30": "float64",  # Cboe's 30-day IV for the underlying (percent points)
        "contracts": "int64",  # listed contracts in the chain
        "cboe_timestamp": "timestamp",  # payload "timestamp" (UTC)
    },
    pk=("as_of", "underlying"),
    partition_col="as_of",
    partition="month",
    write_mode="parts",
)

# Index underlyings to prefix with "_" when OCC's product kind isn't known yet (no
# contract-volume data stored, or `omi chain` on demand).
KNOWN_INDEX = frozenset({"SPX", "SPXW", "XSP", "VIX", "VIX1D", "NDX", "NDXP", "XND", "RUT",
                         "MRUT", "DJX", "OEX", "XEO", "MXEA", "MXEF", "MXACW", "MXUSA"})

REQUIRED_KEYS = {"option", "bid", "ask", "open_interest", "volume"}
OSI_TAIL = 15  # YYMMDD + C/P + 8-digit strike


def _cfg(ctx: RunContext) -> dict:
    return ctx.settings.source(NAME).model_extra or {}


def cboe_symbol(underlying: str, product_kind: str, overrides: dict[str, str]) -> str:
    """URL symbol for an OCC underlying: index options get ``_``; overrides win."""
    if underlying in overrides:
        return overrides[underlying]
    return f"_{underlying}" if product_kind == "OIND" else underlying


def parse_osi(option: pd.Series) -> pd.DataFrame:
    """Split OSI symbols (``SPY260501C00400000``) into root, expiry, cp, strike."""
    s = option.str.strip()
    tail = s.str[-OSI_TAIL:]
    if not tail.str.fullmatch(r"\d{6}[CP]\d{8}").all():
        bad = s[~tail.str.fullmatch(r"\d{6}[CP]\d{8}")].head(3).tolist()
        raise SchemaDrift(f"option symbols not in OSI form, e.g. {bad}")
    return pd.DataFrame({
        "root": s.str[:-OSI_TAIL],
        "expiry": pd.to_datetime(tail.str[:6], format="%y%m%d").dt.date,
        "cp": tail.str[6],
        "strike": tail.str[7:].astype("int64") / 1000.0,
    })


def _et_to_utc(values: pd.Series) -> pd.Series:
    """Cboe's ``last_trade_time`` is ET wall time without an offset."""
    ts = pd.to_datetime(values, errors="coerce")
    return ts.dt.tz_localize(cal.ET, ambiguous="NaT", nonexistent="NaT").dt.tz_convert("UTC")


def parse_chain(raw: bytes, as_of: date, underlying: str) -> dict[str, pd.DataFrame]:
    """Payload -> {chain_eod rows, chain_underlying row}. Pure; used by the source and CLI."""
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        head = raw[:80].decode("utf-8", errors="replace").strip()
        if head.lower().startswith(("<?xml", "<html", "<!doctype")):
            raise NotAvailable(f"Cboe returned a page, not JSON: {head[:60]}") from exc
        raise ParseError(f"not JSON: {exc}") from exc
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict) or "options" not in data:
        raise SchemaDrift(f"no data.options in payload (top-level keys {list(doc)[:8]})")
    options = data["options"] or []
    if not options:
        raise NotAvailable(f"Cboe chain for {underlying} has no contracts")
    df = pd.DataFrame(options)
    missing = REQUIRED_KEYS - set(df.columns)
    if missing:
        raise SchemaDrift(f"contract fields missing: {sorted(missing)}")

    def col(name: str) -> pd.Series:
        if name in df:
            return pd.to_numeric(df[name], errors="coerce")
        return pd.Series(np.nan, index=df.index)

    osi = parse_osi(df["option"])
    iv = col("iv")
    chain = pd.DataFrame({
        "as_of": as_of,
        "underlying": underlying,
        "option": df["option"].str.strip(),
        "root": osi["root"],
        "expiry": osi["expiry"],
        "cp": osi["cp"],
        "strike": osi["strike"],
        "bid": col("bid"),
        "ask": col("ask"),
        "last": col("last_trade_price"),
        "prev_close": col("prev_day_close"),
        "volume": col("volume").fillna(0).round().astype("int64"),
        "open_interest": col("open_interest").fillna(0).round().astype("int64"),
        "iv": iv.where(iv > 0),
        "delta": col("delta"),
        "gamma": col("gamma"),
        "vega": col("vega"),
        "theta": col("theta"),
        "theo": col("theo"),
        "last_trade_at": _et_to_utc(df["last_trade_time"]) if "last_trade_time" in df
        else pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]"),
    })
    # A chain occasionally lists the same OSI twice; keep the row with more open interest.
    chain = (chain.sort_values("open_interest", kind="stable")
             .drop_duplicates(["option"], keep="last").reset_index(drop=True))

    def num(key: str) -> float:
        try:
            return float(data.get(key))
        except (TypeError, ValueError):
            return float("nan")

    stamp = pd.to_datetime(doc.get("timestamp"), errors="coerce")
    under = pd.DataFrame([{
        "as_of": as_of,
        "underlying": underlying,
        "cboe_symbol": str(data.get("symbol") or ""),
        "security_type": str(data.get("security_type") or ""),
        "price": num("current_price"),
        "prev_close": num("prev_day_close"),
        "iv30": num("iv30"),
        "contracts": len(chain),
        # The payload timestamp read 20:27 when fetched at 16:27 ET, so it is UTC.
        "cboe_timestamp": stamp.tz_localize("UTC") if pd.notna(stamp) else pd.NaT,
    }])
    return {CHAIN.name: chain, UNDERLYING.name: under}


def _occ_underlyings(contract_volume: pd.DataFrame) -> pd.DataFrame:
    """underlying, product_kind, sides on the latest stored OCC day, most active first."""
    if contract_volume.empty:
        return pd.DataFrame(columns=["underlying", "product_kind", "sides"])
    df = contract_volume[contract_volume["product_kind"].isin(["OSTK", "OIND"])]
    df = df[df["trade_date"] == df["trade_date"].max()]
    sides = df["cust_sides"] + df["firm_sides"] + df["mm_sides"]
    out = (df.assign(sides=sides).groupby(["underlying", "product_kind"], as_index=False)["sides"]
           .sum().sort_values("sides", ascending=False, kind="stable"))
    return out[out["sides"] > 0]


def _share_class_overrides(listings: pd.DataFrame) -> dict[str, str]:
    """OCC writes BRK.B as BRKB; Cboe URLs use the listed form. Map the ones that differ."""
    if listings.empty or "occ_symbol" not in listings:
        return {}
    snap = listings[listings["as_of"] == listings["as_of"].max()]
    diff = snap[snap["symbol"] != snap["occ_symbol"]]
    return dict(zip(diff["occ_symbol"], diff["symbol"], strict=True))


class CboeChains:
    name = NAME
    tables = (CHAIN, UNDERLYING)
    missing_statuses = (403, 404)

    def in_window(self, session: date, ctx: RunContext) -> bool:
        """True when it is the session's own calendar evening, after ``snapshot_after_et``."""
        cfg = _cfg(ctx)
        if not cfg.get("enforce_window", True):
            return True
        now = (ctx.started_at or cal.now_et()).astimezone(cal.ET)
        after = time.fromisoformat(str(cfg.get("snapshot_after_et", "16:15")))
        return now.date() == session and now.time() >= after

    def symbols(self, ctx: RunContext) -> list[tuple[str, str]]:
        """(OCC underlying, Cboe URL symbol) pairs to snapshot, most important first."""
        cfg = _cfg(ctx)
        scope = str(cfg.get("scope", "all"))
        overrides = {str(k): str(v) for k, v in (cfg.get("symbol_overrides") or {}).items()}
        overrides = {**_share_class_overrides(ctx.read_latest("ref_listed_symbols")), **overrides}
        occ = _occ_underlyings(ctx.read_latest("occ_contract_volume"))
        kinds = dict(zip(occ["underlying"], occ["product_kind"], strict=True))

        pairs: dict[str, str] = {}
        for raw in [*ctx.settings.universe.core, *ctx.settings.universe.extra]:
            sym = raw.strip().upper()
            bare = sym.lstrip("_")
            kind = ("OIND" if sym.startswith("_") or bare in KNOWN_INDEX
                    else kinds.get(bare, "OSTK"))
            sym = bare
            pairs.setdefault(sym, cboe_symbol(sym, kind, overrides))
        if scope == "universe":
            return list(pairs.items())
        ranked = occ
        if scope == "top_n":
            ranked = occ.head(int(cfg.get("top_n", 500)))
        for row in ranked.itertuples(index=False):
            pairs.setdefault(row.underlying, cboe_symbol(row.underlying, row.product_kind,
                                                         overrides))
        if scope == "all":  # keep names snapshotted earlier this month even on a quiet day
            prev = ctx.read_latest(UNDERLYING.name)
            if not prev.empty:
                for row in prev.drop_duplicates("underlying").itertuples(index=False):
                    kind = "OIND" if str(row.cboe_symbol).startswith("^") else "OSTK"
                    pairs.setdefault(row.underlying, cboe_symbol(row.underlying, kind, overrides))
        return list(pairs.items())

    def plan(self, dates: list[date], ctx: RunContext) -> list[FetchRequest]:
        if not dates:
            return []
        session = cal.last_completed_session(ctx.started_at)
        if session not in dates:
            return []
        if not self.in_window(session, ctx):
            log.info("cboe_chains: outside the snapshot window for %s; skipping", session)
            return []
        return [
            FetchRequest(NAME, "options", session, URL.format(sym=url_sym), key=underlying,
                         ext="json")
            for underlying, url_sym in self.symbols(ctx)
        ]

    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]:
        return parse_chain(raw, req.as_of, req.key)

    def after_run(self, ctx: RunContext, storage) -> None:
        """Rolling retention: newest ``retention_sessions`` days of chains, a few of raw JSON."""
        cfg = _cfg(ctx)
        keep = int(cfg.get("retention_sessions", 45))
        dropped = storage.drop_old_partitions(CHAIN, keep)
        if dropped:
            log.info("cboe_chains: dropped %d day(s) beyond %d sessions: %s", len(dropped), keep,
                     ", ".join(dropped))
        keep_raw = int(cfg.get("keep_raw_sessions", 3))
        last = cal.last_completed_session(ctx.started_at)
        cutoff = last
        for _ in range(max(keep_raw - 1, 0)):
            cutoff = cal.previous_session(cutoff)
        removed = storage.prune_raw(NAME, cutoff)
        if removed:
            log.info("cboe_chains: removed %d raw files before %s", removed, cutoff)


def fetch_one(http, underlying: str, url_symbol: str | None = None,
              as_of: date | None = None) -> dict[str, pd.DataFrame]:
    """On-demand chain for one symbol (``omi chain``). Raises on any failure."""
    sym = url_symbol or underlying
    res = http.get(URL.format(sym=sym))
    if not res.ok:
        raise NotAvailable(f"{sym}: {res.error or res.status}")
    as_of = as_of or cal.last_completed_session()
    return parse_chain(res.content, as_of, underlying.lstrip("_").upper())
