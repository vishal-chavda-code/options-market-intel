"""Per-underlying drill-down: who trades it, which expiries, which exchanges, calls vs puts."""

from __future__ import annotations

import components as c
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Options by symbol", page_icon=":material/search:", layout="wide")

OPTION_CLASSES = ("etf", "single_stock", "index", "unclassified_stock")
ACCOUNTS = {"cust_sides": "Customer", "firm_sides": "Firm", "mm_sides": "Market maker"}


def ranked(day) -> pd.DataFrame:
    df = c.query(f"""
        WITH d AS (
            SELECT * FROM {c.feature_sql('underlying_daily')}
            WHERE trade_date = DATE '{day}' AND product_class IN {OPTION_CLASSES}
        )
        SELECT d.underlying, any_value(d.product_class) AS product_class,
               any_value(d.index_scope) AS index_scope, any_value(d.index_family) AS family,
               sum(d.contracts) AS contracts,
               sum(d.cust_sides) / sum(d.cust_sides + d.firm_sides + d.mm_sides) AS customer,
               sum(d.firm_sides) / sum(d.cust_sides + d.firm_sides + d.mm_sides) AS firm,
               sum(d.mm_sides) / sum(d.cust_sides + d.firm_sides + d.mm_sides) AS market_maker,
               sum(d.contracts) FILTER (WHERE d.expiry_bucket = '0DTE') / sum(d.contracts)
                   AS zero_dte
        FROM d GROUP BY d.underlying ORDER BY contracts DESC
    """)
    names = c.query(f"SELECT underlying, product_kind, name "
                    f"FROM {c.feature_sql('underlying_class')}")
    names = names[names.product_kind.isin(["OSTK", "OIND"])].drop_duplicates("underlying")
    df = df.merge(names[["underlying", "name"]], on="underlying", how="left")
    df["class"] = [c.class_label(p, s) for p, s in zip(df.product_class, df.index_scope,
                                                       strict=True)]
    df["zero_dte"] = df["zero_dte"].fillna(0.0)
    df["family"] = df["family"].fillna("")
    return df[["underlying", "name", "class", "family", "contracts", "customer", "firm",
               "market_maker", "zero_dte"]]


def drill_down(symbol: str, start, end) -> None:
    daily = c.query(f"""
        SELECT * FROM {c.feature_sql('underlying_daily')}
        WHERE underlying = '{symbol}' AND trade_date BETWEEN DATE '{start}' AND DATE '{end}'
          AND product_class IN {OPTION_CLASSES}
    """)
    if daily.empty:
        st.info(f"No volume for {symbol} in this range.")
        return
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.date

    left, right = st.columns(2)
    with left:
        st.markdown(f"**{symbol}: trade sides by account type**")
        acct = daily.groupby("trade_date", as_index=False)[list(ACCOUNTS)].sum()
        long = acct.melt("trade_date", var_name="account", value_name="sides")
        long["account"] = long["account"].map(ACCOUNTS)
        fig = c.style(c.bars(long, "trade_date", "sides", "account", c.ACCOUNT_ORDER,
                             c.colour_map(c.ACCOUNT_ORDER)),
                      y_title="trade sides", stacked_bars=True)
        c.chart(fig, acct.rename(columns=ACCOUNTS), key=f"acct_{symbol}",
                caption="Each contract appears twice (buyer and seller side).")
    with right:
        st.markdown(f"**{symbol}: contracts by expiry bucket**")
        exp = daily.groupby(["trade_date", "expiry_bucket", "bucket_order"],
                            as_index=False)["contracts"].sum()
        buckets = list(exp.sort_values("bucket_order")["expiry_bucket"].unique())
        fig = c.style(c.bars(exp, "trade_date", "contracts", "expiry_bucket", buckets,
                             c.ordinal_map(buckets)), y_title="contracts", stacked_bars=True)
        c.chart(fig, exp.drop(columns="bucket_order"), key=f"exp_{symbol}")

    left, right = st.columns(2)
    with left:
        st.markdown(f"**{symbol}: exchange share, {start:%b %d} to {end:%b %d}**")
        venues = c.query(f"""
            SELECT exchange, (sum(cust_sides) + sum(firm_sides) + sum(mm_sides)) / 2.0
                       AS contracts
            FROM {c.table_sql('occ_contract_volume')}
            WHERE underlying = '{symbol}' AND product_kind IN ('OSTK', 'OIND')
              AND trade_date BETWEEN DATE '{start}' AND DATE '{end}'
            GROUP BY exchange ORDER BY contracts
        """)
        venues["exchange"] = venues["exchange"].map(c.exchange_names()).fillna(venues["exchange"])
        venues["share"] = venues["contracts"] / venues["contracts"].sum()
        fig = go.Figure(go.Bar(y=venues["exchange"], x=venues["share"], orientation="h",
                               marker_color=c.colour_map(["x"])["x"],
                               hovertemplate="%{y}: %{x:.1%}<extra></extra>"))
        c.style(fig, y_title="", percent=True, height=max(260, 22 * len(venues)))
        fig.update_xaxes(tickformat=".0%")
        fig.update_layout(hovermode="closest", showlegend=False)
        c.chart(fig, venues.sort_values("contracts", ascending=False), key=f"venue_{symbol}")
    with right:
        st.markdown(f"**{symbol}: calls vs puts**")
        if not c.has_table("occ_symbol_volume"):
            st.info("No call/put data stored yet (source `occ_symbol_volume`).")
        else:
            cp = c.query(f"""
                SELECT trade_date, cp, account_type, sum(sides) AS sides
                FROM {c.table_sql('occ_symbol_volume')}
                WHERE underlying = '{symbol}'
                  AND trade_date BETWEEN DATE '{start}' AND DATE '{end}'
                GROUP BY ALL
            """)
            if cp.empty:
                st.info(f"{symbol} is not in the call/put query set (universe + top-N). "
                        "Add it to `universe.extra` in config to collect it.")
            else:
                cp["series"] = cp["cp"].map({"C": "Calls", "P": "Puts"})
                cp["contracts"] = cp["sides"] / 2
                tot = cp.groupby(["trade_date", "series"], as_index=False)["contracts"].sum()
                fig = c.style(c.bars(tot, "trade_date", "contracts", "series", c.CP_ORDER,
                                     c.colour_map(c.CP_ORDER)),
                              y_title="contracts", stacked_bars=True)
                wide = cp.pivot_table(index="trade_date", columns=["account_type", "cp"],
                                      values="sides", aggfunc="sum")
                wide = wide.reindex(columns=pd.MultiIndex.from_product(
                    [["C", "F", "M"], ["C", "P"]]), fill_value=0).fillna(0)
                pcr = pd.DataFrame({
                    "customer_put_call": wide[("C", "P")] / wide[("C", "C")],
                    "all_put_call": wide.xs("P", axis=1, level=1).sum(axis=1)
                    / wide.xs("C", axis=1, level=1).sum(axis=1),
                }).rename_axis("trade_date").reset_index()
                c.chart(fig, pcr.merge(tot.pivot(index="trade_date", columns="series",
                                                 values="contracts").reset_index()),
                        key=f"cp_{symbol}",
                        caption="All exchanges, all expiries (OCC volume query). The table "
                                "adds put/call ratios for customers and for all accounts.")

    st.markdown(f"**{symbol}: every expiry on {end:%b %d} (finest grain stored)**")
    detail = c.query(f"""
        SELECT expiry, datediff('day', trade_date, expiry) AS days_to_expiry,
               string_agg(DISTINCT symbol, ' ') AS roots,
               sum(cust_sides) AS customer_sides, sum(firm_sides) AS firm_sides,
               sum(mm_sides) AS market_maker_sides,
               (sum(cust_sides) + sum(firm_sides) + sum(mm_sides)) / 2.0 AS contracts,
               count(DISTINCT exchange) AS exchanges
        FROM {c.table_sql('occ_contract_volume')}
        WHERE underlying = '{symbol}' AND trade_date = DATE '{end}'
          AND product_kind IN ('OSTK', 'OIND')
        GROUP BY expiry, trade_date ORDER BY expiry
    """)
    detail["expiry"] = pd.to_datetime(detail["expiry"]).dt.date
    counts = {col: st.column_config.NumberColumn(col, format="localized")
              for col in ("customer_sides", "firm_sides", "market_maker_sides", "contracts")}
    st.dataframe(detail, width="stretch", hide_index=True, column_config=counts)
    st.download_button("Download CSV", detail.to_csv(index=False).encode(),
                       f"{symbol}_{end}_expiries.csv", "text/csv", key=f"dl_detail_{symbol}")


def render() -> None:
    st.title("Options by symbol")
    st.caption("Every optionable underlying, all U.S. exchanges (OCC). Select a row to drill "
               "down. Shares are of trade sides; contracts = sides / 2.")
    c.freshness(["occ_contract_volume", "occ_symbol_volume"])
    if not c.has_feature("underlying_daily"):
        st.info("No contract-volume data yet. Run `omi backfill`, then `omi features`.")
        return

    days = c.query(f"SELECT DISTINCT trade_date FROM {c.feature_sql('underlying_daily')} "
                   "ORDER BY trade_date")["trade_date"]
    start, end = c.date_range(days, "sym_range")
    classes = st.sidebar.multiselect("Product class", c.CLASS_ORDER, default=c.CLASS_ORDER[:4])
    search = st.sidebar.text_input("Find symbol", placeholder="e.g. NVDA").strip().upper()

    table = ranked(end)
    table = table[table["class"].isin(classes)]
    if search:
        table = table[table["underlying"].str.contains(search, regex=False)]
    st.markdown(f"**Underlyings ranked by contracts on {end:%a %b %d, %Y}** "
                f"({len(table):,} shown)")
    event = st.dataframe(
        table, width="stretch", hide_index=True, height=360,
        on_select="rerun", selection_mode="single-row", key="ranked",
        column_config={
            "contracts": st.column_config.NumberColumn("contracts", format="localized"),
            "customer": st.column_config.ProgressColumn("customer", format="percent",
                                                        min_value=0, max_value=1),
            "firm": st.column_config.NumberColumn("firm", format="percent"),
            "market_maker": st.column_config.NumberColumn("market maker", format="percent"),
            "zero_dte": st.column_config.NumberColumn("0DTE share", format="percent"),
        },
    )
    st.download_button("Download CSV", table.to_csv(index=False).encode(), f"ranked_{end}.csv",
                       "text/csv", key="dl_ranked")

    picked = event.selection.rows if event and event.selection else []
    default = table.iloc[picked[0]]["underlying"] if picked else (
        search if search in set(table["underlying"]) else
        (table.iloc[0]["underlying"] if not table.empty else None))
    if default:
        st.divider()
        drill_down(default, start, end)


c.run_page(render)
