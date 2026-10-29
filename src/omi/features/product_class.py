"""Product-class features: ETF vs single-stock vs index options (broad / narrow), per day.

Classification of each (underlying, OCC product kind):
  * OIND -> ``index``; ``index_scope`` (broad / narrow / unclassified) and ``index_family``
    (S&P 500, VIX, Russell 2000...) from config/reference/index_underlyings.csv. VIX options
    are index options; the family lets the dashboard break them out.
  * OSTK -> ``etf`` or ``single_stock`` by the ETF flag in the latest Nasdaq Trader symbol
    directory snapshot; ``unclassified_stock`` if the symbol is not listed there
    (delisted, bankrupt "Q" names...).
  * OCHF -> ``futures`` and OOOF -> ``futures_option`` (in OCC's file, not options volume
    proper; excluded from the dashboard's options totals).

Volumes are trade **sides** (customer + firm + market maker = 2 x contracts);
``contracts`` = sides / 2. Customer share = customer sides / all sides.

Outputs (data/features/, rebuilt from scratch each run):
  underlying_class.parquet   underlying, product_kind -> class, scope, family, name
  underlying_daily.parquet   trade_date x underlying x expiry bucket
  class_daily.parquet        trade_date x class x scope x family
  class_exchange_daily.parquet  trade_date x class x exchange
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import duckdb
import pandas as pd

from omi.config import REFERENCE_DIR, Settings
from omi.storage import Storage

log = logging.getLogger(__name__)

OPTION_CLASSES = ("etf", "single_stock", "index", "unclassified_stock")


def bucket_sql(bounds: list[int], dte: str = "dte") -> tuple[str, str]:
    """SQL CASE expressions (label, sort order) for days-to-expiry buckets.

    ``[0, 7, 30, 90]`` -> 0DTE, 1-7d, 8-30d, 31-90d, 91d+.
    """
    labels, orders, lo = [], [], 0
    for i, b in enumerate(sorted(bounds)):
        label = "0DTE" if lo == 0 and b == 0 else f"{lo}-{b}d"
        labels.append(f"WHEN {dte} <= {b} THEN '{label}'")
        orders.append(f"WHEN {dte} <= {b} THEN {i}")
        lo = b + 1
    label_sql = f"CASE {' '.join(labels)} ELSE '{lo}d+' END"
    order_sql = f"CASE {' '.join(orders)} ELSE {len(bounds)} END"
    return label_sql, order_sql


def _copy(con: duckdb.DuckDBPyConnection, sql: str, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    con.execute(f"COPY ({sql}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    os.replace(tmp, path)
    return con.execute(f"SELECT count(*) FROM read_parquet('{path.as_posix()}')").fetchone()[0]


def build(
    storage: Storage, settings: Settings, reference_dir: Path = REFERENCE_DIR
) -> dict[str, int]:
    """Rebuild the product-class feature files. Returns rows written per file."""
    if not storage.has_table("occ_contract_volume"):
        log.warning("features: no occ_contract_volume data yet; nothing to build")
        return {}
    con = duckdb.connect()
    con.execute(f"CREATE VIEW cv AS SELECT * FROM read_parquet("
                f"'{storage.table_glob('occ_contract_volume')}', "
                "union_by_name = true, hive_partitioning = false)")

    if storage.has_table("ref_listed_symbols"):
        con.execute(f"""
            CREATE TABLE listings AS
            WITH snap AS (
                SELECT * FROM read_parquet('{storage.table_glob('ref_listed_symbols')}',
                                           hive_partitioning = false)
                WHERE NOT is_test_issue
            )
            SELECT occ_symbol, bool_or(is_etf) AS is_etf, any_value(name) AS name
            FROM snap WHERE as_of = (SELECT max(as_of) FROM snap)
            GROUP BY occ_symbol
        """)
    else:
        log.warning("features: no symbol directory yet; ETFs cannot be told from stocks")
        con.execute("CREATE TABLE listings (occ_symbol VARCHAR, is_etf BOOLEAN, name VARCHAR)")

    index_ref = pd.read_csv(reference_dir / "index_underlyings.csv", dtype=str).fillna("")
    con.register("index_ref", index_ref)
    con.execute("""
        CREATE TABLE classes AS
        SELECT u.underlying, u.product_kind,
            CASE
                WHEN u.product_kind = 'OIND' THEN 'index'
                WHEN u.product_kind = 'OCHF' THEN 'futures'
                WHEN u.product_kind = 'OOOF' THEN 'futures_option'
                WHEN l.is_etf THEN 'etf'
                WHEN l.is_etf IS NOT NULL THEN 'single_stock'
                ELSE 'unclassified_stock'
            END AS product_class,
            CASE WHEN u.product_kind = 'OIND'
                 THEN coalesce(nullif(r.scope, ''), 'unclassified') END AS index_scope,
            CASE WHEN u.product_kind = 'OIND'
                 THEN coalesce(nullif(r.family, ''), 'Other') END AS index_family,
            coalesce(l.name, nullif(r.name, '')) AS name
        FROM (SELECT DISTINCT underlying, product_kind FROM cv) u
        LEFT JOIN listings l ON u.product_kind = 'OSTK' AND l.occ_symbol = u.underlying
        LEFT JOIN index_ref r ON u.product_kind = 'OIND' AND r.underlying = u.underlying
    """)

    label_sql, order_sql = bucket_sql(settings.features.expiry_buckets)
    out = storage.features_dir
    counts = {
        "underlying_class": _copy(con, "SELECT * FROM classes ORDER BY underlying",
                                  out / "underlying_class.parquet"),
        "underlying_daily": _copy(con, f"""
            WITH x AS (SELECT *, datediff('day', trade_date, expiry) AS dte FROM cv)
            SELECT x.trade_date, x.underlying, c.product_class, c.index_scope, c.index_family,
                   {label_sql} AS expiry_bucket, {order_sql} AS bucket_order,
                   sum(cust_sides) AS cust_sides, sum(firm_sides) AS firm_sides,
                   sum(mm_sides) AS mm_sides,
                   (sum(cust_sides) + sum(firm_sides) + sum(mm_sides)) / 2.0 AS contracts
            FROM x JOIN classes c USING (underlying, product_kind)
            GROUP BY ALL ORDER BY trade_date, underlying, bucket_order
        """, out / "underlying_daily.parquet"),
        "class_daily": _copy(con, """
            SELECT cv.trade_date, c.product_class, c.index_scope, c.index_family,
                   sum(cust_sides) AS cust_sides, sum(firm_sides) AS firm_sides,
                   sum(mm_sides) AS mm_sides,
                   (sum(cust_sides) + sum(firm_sides) + sum(mm_sides)) / 2.0 AS contracts,
                   count(DISTINCT cv.underlying) AS underlyings
            FROM cv JOIN classes c USING (underlying, product_kind)
            GROUP BY ALL ORDER BY trade_date, product_class
        """, out / "class_daily.parquet"),
        "class_exchange_daily": _copy(con, """
            SELECT cv.trade_date, c.product_class, cv.exchange,
                   sum(cust_sides) AS cust_sides, sum(firm_sides) AS firm_sides,
                   sum(mm_sides) AS mm_sides,
                   (sum(cust_sides) + sum(firm_sides) + sum(mm_sides)) / 2.0 AS contracts
            FROM cv JOIN classes c USING (underlying, product_kind)
            GROUP BY ALL ORDER BY trade_date, product_class, exchange
        """, out / "class_exchange_daily.parquet"),
    }
    con.close()
    return counts
