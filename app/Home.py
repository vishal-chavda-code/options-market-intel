"""Options volume by product class: ETF vs single stock vs index (broad / narrow)."""

from __future__ import annotations

import components as c
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Options product mix", page_icon=":material/stacked_bar_chart:",
                   layout="wide")

OPTION_CLASSES = ("etf", "single_stock", "index", "unclassified_stock")


def tiles(by_class: pd.DataFrame, index_family: pd.DataFrame) -> None:
    days = sorted(by_class["trade_date"].unique())
    last, prev = days[-1], (days[-2] if len(days) > 1 else None)

    def total(d):
        return by_class.loc[by_class.trade_date == d, "contracts"].sum()

    def share(d, labels):
        t = by_class[by_class.trade_date == d]
        return t.loc[t["class"].isin(labels), "contracts"].sum() / t["contracts"].sum()

    def cust(d):
        t = by_class[by_class.trade_date == d]
        return t["cust_sides"].sum() / t[["cust_sides", "firm_sides", "mm_sides"]].sum().sum()

    def vix(d):
        t = index_family[(index_family.trade_date == d) & (index_family.family == "VIX")]
        return t["contracts"].sum()

    def pp(fn, labels=None):
        if prev is None:
            return None
        now, before = (fn(last, labels), fn(prev, labels)) if labels else (fn(last), fn(prev))
        return f"{(now - before) * 100:+.1f} pp vs {prev:%b %d}"

    st.subheader(f"{last:%A %b %d, %Y}")
    cols = st.columns(6)
    cols[0].metric("Options contracts", f"{total(last) / 1e6:,.1f}M",
                   f"{total(last) / total(prev) - 1:+.1%} vs {prev:%b %d}" if prev else None,
                   delta_color="off")
    cols[1].metric("ETF share", f"{share(last, ['ETF']):.1%}", pp(share, ["ETF"]),
                   delta_color="off")
    cols[2].metric("Single-stock share", f"{share(last, ['Single stock']):.1%}",
                   pp(share, ["Single stock"]), delta_color="off")
    idx = ["Index, broad", "Index, narrow"]
    cols[3].metric("Index share", f"{share(last, idx):.1%}", pp(share, idx), delta_color="off")
    cols[4].metric("VIX option contracts", f"{vix(last):,.0f}",
                   f"{vix(last) / vix(prev) - 1:+.1%} vs {prev:%b %d}" if prev and vix(prev)
                   else None, delta_color="off")
    cols[5].metric("Customer share of sides", f"{cust(last):.1%}", pp(cust),
                   delta_color="off",
                   help="Customer sides / all sides (customer + firm + market maker).")


def render() -> None:
    st.title("Options volume by product class")
    st.caption(
        "All U.S. options exchanges, from OCC cleared volume. OCC reports both sides of each "
        "trade; **contracts = sides / 2**. ETF vs single stock from the Nasdaq Trader symbol "
        "directory; index broad / narrow from `config/reference/index_underlyings.csv`. "
        "VIX options are index options (broad), broken out by family below."
    )
    c.freshness(["occ_contract_volume", "symbol_directory", "occ_daily_volume"])
    if not c.has_feature("class_daily"):
        st.info("No contract-volume data yet. Run `omi backfill`, then `omi features`.")
        return

    raw = c.query(f"SELECT * FROM {c.feature_sql('class_daily')}")
    raw = raw[raw["product_class"].isin(OPTION_CLASSES)].copy()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"]).dt.date
    raw["class"] = [c.class_label(p, s) for p, s in zip(raw.product_class, raw.index_scope,
                                                        strict=True)]
    start, end = c.date_range(raw["trade_date"], "home_range")
    df = raw[(raw.trade_date >= start) & (raw.trade_date <= end)]
    sums = ["contracts", "cust_sides", "firm_sides", "mm_sides"]
    by_class = df.groupby(["trade_date", "class"], as_index=False)[sums].sum()

    fam = df[df.product_class == "index"].copy()
    fam["family"] = fam["index_family"].where(fam["index_family"].isin(c.FAMILY_ORDER), "Other")
    by_family = fam.groupby(["trade_date", "family"], as_index=False)["contracts"].sum()

    tiles(by_class, by_family)
    class_colours = c.colour_map(c.CLASS_ORDER)

    left, right = st.columns(2)
    with left:
        st.markdown("**Contracts per day by product class**")
        fig = c.style(c.bars(by_class, "trade_date", "contracts", "class", c.CLASS_ORDER,
                             class_colours), y_title="contracts", stacked_bars=True)
        c.chart(fig, by_class.sort_values(["trade_date", "class"]), key="class_daily",
                caption="Other = unclassified index products and stocks missing from the "
                        "symbol directory (tiny).")
    with right:
        st.markdown("**Customer share of trade sides**")
        share = by_class.copy()
        share["customer_share"] = share["cust_sides"] / share[["cust_sides", "firm_sides",
                                                               "mm_sides"]].sum(axis=1)
        shown = ["ETF", "Single stock", "Index, broad"]
        fig = c.style(c.lines(share, "trade_date", "customer_share", "class", shown,
                              class_colours, hover_fmt=".1%"),
                      y_title="customer / all sides", percent=True)
        fig.update_yaxes(rangemode="normal")
        c.chart(fig, share[share["class"].isin(shown)][["trade_date", "class", "customer_share"]],
                key="customer_share")

    left, right = st.columns(2)
    with left:
        st.markdown("**Index options by family**")
        fig = c.style(c.bars(by_family, "trade_date", "contracts", "family", c.FAMILY_ORDER,
                             c.colour_map(c.FAMILY_ORDER)),
                      y_title="contracts", stacked_bars=True)
        c.chart(fig, by_family.sort_values(["trade_date", "family"]), key="index_family",
                caption="Other = Dow, S&P 100, MSCI, sector and unclassified index products.")
    with right:
        st.markdown(f"**Expiry mix, {start:%b %d} to {end:%b %d}**")
        mix = c.query(f"""
            SELECT product_class, index_scope, expiry_bucket, bucket_order,
                   sum(contracts) AS contracts
            FROM {c.feature_sql('underlying_daily')}
            WHERE trade_date BETWEEN DATE '{start}' AND DATE '{end}'
              AND product_class IN {OPTION_CLASSES}
            GROUP BY ALL
        """)
        mix["class"] = [c.class_label(p, s) for p, s in zip(mix.product_class, mix.index_scope,
                                                            strict=True)]
        mix = mix[mix["class"] != "Other"]
        mix = mix.groupby(["class", "expiry_bucket", "bucket_order"], as_index=False)[
            "contracts"].sum()
        mix["share"] = mix["contracts"] / mix.groupby("class")["contracts"].transform("sum")
        buckets = list(mix.sort_values("bucket_order")["expiry_bucket"].unique())
        colours = c.ordinal_map(buckets)
        fig = go.Figure()
        rows = [k for k in c.CLASS_ORDER if k in set(mix["class"])][::-1]
        for b in buckets:
            part = mix[mix.expiry_bucket == b].set_index("class").reindex(rows)
            fig.add_bar(y=rows, x=part["share"], name=b, orientation="h",
                        marker_color=colours[b],
                        hovertemplate=f"{b}: %{{x:.1%}}<extra>%{{y}}</extra>")
        fig.update_layout(barmode="stack", hovermode="closest")
        c.style(fig, y_title="", percent=True, stacked_bars=True)
        fig.update_xaxes(tickformat=".0%")
        fig.update_layout(hovermode="closest")
        c.chart(fig, mix.sort_values(["class", "bucket_order"]).drop(columns="bucket_order"),
                key="expiry_mix", caption="Calendar days to expiry on the trade date. "
                                          "0DTE = expires the day it trades.")

    if c.has_table("occ_daily_volume"):
        st.divider()
        st.markdown("**Longer history: OCC daily totals (equity includes ETFs here)**")
        hist = c.query(f"SELECT trade_date, equity, index_other FROM "
                       f"{c.table_sql('occ_daily_volume')} ORDER BY trade_date")
        long = hist.melt("trade_date", var_name="series", value_name="contracts")
        long["series"] = long["series"].map({"equity": "Equity incl. ETF",
                                             "index_other": "Index"})
        base = c.colour_map(c.WEEKLY_CLASS_ORDER)
        colours = {"Equity incl. ETF": base["Single stock"], "Index": base["Index"]}
        fig = c.style(c.lines(long, "trade_date", "contracts", "series",
                              ["Equity incl. ETF", "Index"], colours),
                      y_title="contracts per day", height=300)
        fig.update_traces(mode="lines")
        c.chart(fig, hist, key="occ_daily_history",
                caption="This OCC file does not separate ETFs from single stocks. For the "
                        "ETF / single-stock split over two years, see the Weekly page.")


c.run_page(render)
