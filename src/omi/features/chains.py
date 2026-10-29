"""Open-interest features from the stored option-chain snapshots (``chain_eod``).

Outputs (data/features/, rebuilt from scratch each run; empty set if no chains stored):
  chain_underlying_daily.parquet  as_of x underlying: call/put/total OI and volume, P/C,
                                  estimated premium traded and OI notional, OI change vs the
                                  previous snapshot and vs 5 snapshots back, product class
  chain_contract_changes.parquet  latest snapshot vs the one before, per contract: OI then
                                  and now, change, and the change in notional
  chain_report_latest.parquet     one row per ranked item for the report / slide (top OI
                                  builds and unwinds by underlying, top contract changes)

Definitions:
  * ``mark`` = bid/ask mid when both sides are quoted, else last trade price, else Cboe theo.
  * ``premium_est`` = sum(volume x mark x 100): the session's traded premium, *estimated*
    with closing marks (not trade prices). Index and equity options use a 100 multiplier;
    a few products differ (mini/adjusted options), so treat it as an approximation.
  * ``oi_notional`` = sum(open_interest x mark x 100): market value of open positions.
  * OI changes compare consecutive **stored snapshots**; if a day is missing the change spans
    the gap (``prev_as_of`` says which day it was compared to).
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from omi.config import Settings
from omi.features.product_class import _copy
from omi.storage import Storage

log = logging.getLogger(__name__)

MARK_SQL = """CASE WHEN bid > 0 AND ask > 0 THEN (bid + ask) / 2
                   WHEN last > 0 THEN last ELSE coalesce(theo, 0) END"""
TOP_N = 25


def build(storage: Storage, settings: Settings) -> dict[str, int]:
    """Rebuild the chain feature files. Returns rows written per file."""
    if not storage.has_table("chain_eod"):
        return {}
    out_dir: Path = storage.features_dir
    con = duckdb.connect()
    con.execute(f"""CREATE VIEW ce AS SELECT *, {MARK_SQL} AS mark FROM read_parquet(
        '{storage.table_glob('chain_eod')}', union_by_name = true, hive_partitioning = false)""")
    if storage.has_table("chain_underlying"):
        con.execute(f"""CREATE VIEW cu AS SELECT * FROM read_parquet(
            '{storage.table_glob('chain_underlying')}', union_by_name = true,
            hive_partitioning = false)""")
    else:
        con.execute("CREATE VIEW cu AS SELECT NULL::DATE AS as_of, NULL::VARCHAR AS underlying, "
                    "NULL::DOUBLE AS price, NULL::DOUBLE AS iv30 WHERE false")
    classes = out_dir / "underlying_class.parquet"
    if classes.exists():
        con.execute(f"""CREATE VIEW cls AS
            SELECT underlying, any_value(product_class) AS product_class,
                   any_value(index_scope) AS index_scope, any_value(name) AS name
            FROM read_parquet('{classes.as_posix()}')
            WHERE product_kind IN ('OSTK', 'OIND') GROUP BY underlying""")
    else:
        con.execute("CREATE VIEW cls AS SELECT NULL::VARCHAR AS underlying, "
                    "NULL::VARCHAR AS product_class, NULL::VARCHAR AS index_scope, "
                    "NULL::VARCHAR AS name WHERE false")

    con.execute("""
        CREATE TABLE und AS
        WITH agg AS (
            SELECT as_of, underlying,
                sum(open_interest) FILTER (WHERE cp = 'C') AS call_oi,
                sum(open_interest) FILTER (WHERE cp = 'P') AS put_oi,
                sum(open_interest) AS total_oi,
                sum(volume) FILTER (WHERE cp = 'C') AS call_vol,
                sum(volume) FILTER (WHERE cp = 'P') AS put_vol,
                sum(volume) AS total_vol,
                sum(volume * mark * 100) AS premium_est,
                sum(open_interest * mark * 100) AS oi_notional,
                count(*) AS contracts,
                count(DISTINCT expiry) AS expiries
            FROM ce GROUP BY as_of, underlying
        ), lagged AS (
            SELECT *,
                lag(as_of) OVER w AS prev_as_of,
                lag(total_oi) OVER w AS prev_total_oi,
                lag(total_oi, 5) OVER w AS total_oi_5back
            FROM agg WINDOW w AS (PARTITION BY underlying ORDER BY as_of)
        )
        SELECT l.as_of, l.underlying, c.name, c.product_class, c.index_scope,
            u.price, u.iv30,
            coalesce(l.call_oi, 0) AS call_oi, coalesce(l.put_oi, 0) AS put_oi, l.total_oi,
            coalesce(l.put_oi, 0) / nullif(l.call_oi, 0) AS pc_oi,
            coalesce(l.call_vol, 0) AS call_vol, coalesce(l.put_vol, 0) AS put_vol, l.total_vol,
            coalesce(l.put_vol, 0) / nullif(l.call_vol, 0) AS pc_vol,
            l.total_vol / nullif(l.total_oi, 0) AS vol_to_oi,
            l.premium_est, l.oi_notional, l.contracts, l.expiries,
            l.prev_as_of, l.total_oi - l.prev_total_oi AS d_oi,
            (l.total_oi - l.prev_total_oi) / nullif(l.prev_total_oi, 0) AS d_oi_pct,
            l.total_oi - l.total_oi_5back AS d_oi_5,
            (l.total_oi - l.total_oi_5back) / nullif(l.total_oi_5back, 0) AS d_oi_5_pct
        FROM lagged l
        LEFT JOIN cls c USING (underlying)
        LEFT JOIN cu u ON u.as_of = l.as_of AND u.underlying = l.underlying
    """)

    snaps = [r[0] for r in con.execute(
        "SELECT DISTINCT as_of FROM ce ORDER BY as_of DESC LIMIT 2").fetchall()]
    if len(snaps) == 2:
        now, prev = snaps
        con.execute(f"""
            CREATE TABLE chg AS
            WITH a AS (SELECT * FROM ce WHERE as_of = DATE '{now}'),
                 b AS (SELECT * FROM ce WHERE as_of = DATE '{prev}')
            SELECT DATE '{now}' AS as_of, DATE '{prev}' AS prev_as_of,
                coalesce(a.underlying, b.underlying) AS underlying,
                coalesce(a.option, b.option) AS option,
                coalesce(a.root, b.root) AS root, coalesce(a.expiry, b.expiry) AS expiry,
                coalesce(a.cp, b.cp) AS cp, coalesce(a.strike, b.strike) AS strike,
                coalesce(b.open_interest, 0) AS oi_prev,
                coalesce(a.open_interest, 0) AS oi_now,
                coalesce(a.open_interest, 0) - coalesce(b.open_interest, 0) AS d_oi,
                coalesce(a.volume, 0) AS volume,
                coalesce(a.mark, b.mark) AS mark,
                (coalesce(a.open_interest, 0) - coalesce(b.open_interest, 0))
                    * coalesce(a.mark, b.mark) * 100 AS d_oi_notional,
                a.iv, a.delta
            FROM a FULL OUTER JOIN b ON a.underlying = b.underlying AND a.option = b.option
            WHERE coalesce(a.open_interest, 0) <> coalesce(b.open_interest, 0)
        """)
    else:
        con.execute("""CREATE TABLE chg AS SELECT NULL::DATE AS as_of, NULL::DATE AS prev_as_of,
            NULL::VARCHAR AS underlying, NULL::VARCHAR AS option, NULL::VARCHAR AS root,
            NULL::DATE AS expiry, NULL::VARCHAR AS cp, NULL::DOUBLE AS strike,
            NULL::BIGINT AS oi_prev, NULL::BIGINT AS oi_now, NULL::BIGINT AS d_oi,
            NULL::BIGINT AS volume, NULL::DOUBLE AS mark, NULL::DOUBLE AS d_oi_notional,
            NULL::DOUBLE AS iv, NULL::DOUBLE AS delta WHERE false""")

    counts = {
        "chain_underlying_daily": _copy(con, "SELECT * FROM und ORDER BY as_of, underlying",
                                        out_dir / "chain_underlying_daily.parquet"),
        "chain_contract_changes": _copy(con, "SELECT * FROM chg ORDER BY abs(d_oi) DESC",
                                        out_dir / "chain_contract_changes.parquet"),
        "chain_report_latest": _copy(con, f"""
            WITH latest AS (SELECT * FROM und WHERE as_of = (SELECT max(as_of) FROM und)
                                                AND d_oi IS NOT NULL),
            builds AS (SELECT 'oi_build' AS section, row_number() OVER (ORDER BY d_oi DESC) AS rank,
                              * FROM latest WHERE d_oi > 0 QUALIFY rank <= {TOP_N}),
            unwinds AS (SELECT 'oi_unwind' AS section, row_number() OVER (ORDER BY d_oi) AS rank,
                               * FROM latest WHERE d_oi < 0 QUALIFY rank <= {TOP_N})
            SELECT * FROM builds UNION ALL SELECT * FROM unwinds ORDER BY section, rank
        """, out_dir / "chain_report_latest.parquet"),
    }
    return counts
