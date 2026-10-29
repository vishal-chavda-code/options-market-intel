"""Cboe Options exchange statistics: open interest and put/call ratios by category."""

from __future__ import annotations

import components as c
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Cboe OI and P/C", page_icon=":material/donut_small:",
                   layout="wide")

CATEGORIES = {"EXCHANGE TRADED PRODUCTS": "ETF", "EQUITY OPTIONS": "Single stock",
              "INDEX OPTIONS": "Index"}
RATIOS = {"EXCHANGE TRADED PRODUCTS PUT/CALL RATIO": "ETF", "EQUITY PUT/CALL RATIO": "Single stock",
          "INDEX PUT/CALL RATIO": "Index", "CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO": "VIX",
          "TOTAL PUT/CALL RATIO": "Total"}


def render() -> None:
    st.title("Cboe exchange: open interest and put/call")
    st.caption("Cboe Options exchange only (one of ~20 U.S. options exchanges), but the only "
               "free daily source here with **open interest** by category. Cboe's \"Equity\" "
               "excludes ETFs; Index includes SPX and VIX.")
    c.freshness(["cboe_daily"])
    if not c.has_table("cboe_daily_volume"):
        st.info("No Cboe data yet. Run `omi backfill -s cboe_daily`.")
        return

    vol = c.query(f"SELECT * FROM {c.table_sql('cboe_daily_volume')}")
    vol["trade_date"] = pd.to_datetime(vol["trade_date"]).dt.date
    start, end = c.date_range(vol["trade_date"], "cboe_range")
    vol = vol[(vol.trade_date >= start) & (vol.trade_date <= end)]
    vol = vol[vol["category"].isin(CATEGORIES)].copy()
    vol["class"] = vol["category"].map(CATEGORIES)
    order = c.WEEKLY_CLASS_ORDER
    colours = c.colour_map(order)

    left, right = st.columns(2)
    with left:
        st.markdown("**Open interest (calls + puts)**")
        fig = c.style(c.lines(vol, "trade_date", "total_oi", "class", order, colours),
                      y_title="open contracts")
        fig.update_traces(mode="lines")
        c.chart(fig, vol[["trade_date", "class", "call_oi", "put_oi", "total_oi"]],
                key="cboe_oi")
    with right:
        st.markdown("**Volume (calls + puts)**")
        fig = c.style(c.bars(vol, "trade_date", "total_vol", "class", order, colours),
                      y_title="contracts", stacked_bars=True)
        c.chart(fig, vol[["trade_date", "class", "call_vol", "put_vol", "total_vol"]],
                key="cboe_vol")

    ratios = c.query(f"SELECT * FROM {c.table_sql('cboe_pc_ratios')}")
    ratios["trade_date"] = pd.to_datetime(ratios["trade_date"]).dt.date
    ratios = ratios[(ratios.trade_date >= start) & (ratios.trade_date <= end)]
    ratios = ratios[ratios["ratio_name"].isin(RATIOS)].copy()
    ratios["series"] = ratios["ratio_name"].map(RATIOS)
    pc_order = ["ETF", "Single stock", "Index", "VIX", "Total"]
    pc_colours = c.colour_map(pc_order)  # first three match the charts above
    st.markdown("**Put/call volume ratios**")
    fig = c.style(c.lines(ratios, "trade_date", "value", "series", pc_order, pc_colours,
                          hover_fmt=".2f"), y_title="puts / calls")
    fig.update_traces(mode="lines")
    fig.update_yaxes(tickformat=".1f")
    c.chart(fig, ratios[["trade_date", "series", "value"]], key="cboe_pc")


c.run_page(render)
