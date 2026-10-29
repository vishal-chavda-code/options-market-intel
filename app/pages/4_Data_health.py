"""Data health: per-source freshness, recent errors, and how much archive has been built."""

from __future__ import annotations

import components as c
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Data health", page_icon=":material/monitor_heart:",
                   layout="wide")

TABLES = {
    "occ_contract_volume": "trade_date", "occ_symbol_volume": "trade_date",
    "occ_weekly_class": "week_ending", "occ_daily_volume": "trade_date",
    "cboe_daily_volume": "trade_date", "ref_listed_symbols": "as_of",
}


def coverage() -> pd.DataFrame:
    rows = []
    for name, col in TABLES.items():
        if not c.has_table(name):
            rows.append({"table": name, "first": None, "last": None, "dates": 0, "rows": 0,
                         "size_mb": 0.0})
            continue
        stats = c.query(f"SELECT CAST(min({col}) AS VARCHAR) AS first, "
                        f"CAST(max({col}) AS VARCHAR) AS last, "
                        f"count(DISTINCT {col}) AS dates, count(*) AS rows "
                        f"FROM {c.table_sql(name)}").iloc[0]
        size = sum(p.stat().st_size for p in (c.data_dir() / "tables" / name).rglob("*.parquet"))
        rows.append({"table": name, "first": stats["first"], "last": stats["last"],
                     "dates": int(stats["dates"]), "rows": int(stats["rows"]),
                     "size_mb": round(size / 1e6, 1)})
    return pd.DataFrame(rows)


def render() -> None:
    st.title("Data health")
    h = c.health()
    if h.empty:
        st.info("Nothing fetched yet. Run `omi probe`, then `omi backfill`.")
        return
    c.freshness(list(dict.fromkeys(h["source"])))

    st.markdown("**Per source**")
    st.dataframe(h.fillna(""), width="stretch", hide_index=True)

    st.markdown("**Archive coverage**")
    cov = coverage()
    st.dataframe(cov, width="stretch", hide_index=True)
    st.caption("OCC keeps only ~30 sessions of the contract-volume file, so `first` for "
               "occ_contract_volume only moves earlier while the daily job keeps running.")

    st.markdown("**Open problems** (requests that have never succeeded)")
    log_glob = (c.data_dir() / "meta" / "ingest_log" / "*.parquet").as_posix()
    log = c.query(f"""
        WITH log AS (SELECT * FROM read_parquet('{log_glob}')),
        latest AS (
            SELECT *, row_number() OVER (PARTITION BY source, report, as_of_date, key
                                         ORDER BY finished_at DESC) AS rn
            FROM log
        )
        SELECT CAST(finished_at AS VARCHAR) AS last_attempt, source, report,
               CAST(as_of_date AS VARCHAR) AS as_of, key, status, error_msg
        FROM latest
        WHERE rn = 1 AND status <> 'ok'
          AND NOT EXISTS (
              SELECT 1 FROM log o
              WHERE o.status = 'ok' AND o.source = latest.source AND o.report = latest.report
                AND o.as_of_date = latest.as_of_date
                AND coalesce(o.key, '') = coalesce(latest.key, ''))
        ORDER BY as_of DESC, source LIMIT 200
    """)
    st.dataframe(log, width="stretch", hide_index=True)
    st.caption("`not_available` is normal for today's date before the source publishes, and "
               "for dates outside a source's history window; it is retried on the next run.")


c.run_page(render)
