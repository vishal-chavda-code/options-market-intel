"""OCC weekly report by class: two years of ETF vs single-stock vs index, opening vs closing."""

from __future__ import annotations

import components as c
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Weekly by class", page_icon=":material/calendar_view_week:",
                   layout="wide")

CLASS_NAMES = {"etf": "ETF", "equity": "Single stock", "index": "Index"}


def load() -> pd.DataFrame:
    df = c.query(f"SELECT * FROM {c.table_sql('occ_weekly_class')}")
    df["week_ending"] = pd.to_datetime(df["week_ending"]).dt.date
    df["class"] = df["product_class"].map(CLASS_NAMES)
    df["buys"] = df["open_buy_contracts"] + df["close_buy_contracts"]
    df["sells"] = df["open_sell_contracts"] + df["close_sell_contracts"]
    df["opening"] = df["open_buy_contracts"] + df["open_sell_contracts"]
    df["premium"] = df["open_buy_premium"] + df["close_buy_premium"]
    return df


def render() -> None:
    st.title("Weekly volume by product class")
    st.caption("OCC Weekly Volume Report, all U.S. exchanges. **Single stock** is OCC's "
               "\"equity\" class (ETFs are reported separately). Index includes VIX. "
               "Weeks are dated by their Friday, as OCC reports them (even when Friday is "
               "a market holiday).")
    c.freshness(["occ_weekly_class"])
    if not c.has_table("occ_weekly_class"):
        st.info("No weekly data yet. Run `omi backfill -s occ_weekly_class`.")
        return

    df = load()
    flex = st.sidebar.radio("Options", ["Standard", "FLEX"], horizontal=True) == "FLEX"
    side = st.sidebar.radio("Calls / puts", ["Combined", "Calls", "Puts"], horizontal=True)
    df = df[(df.flex == flex) & (df.option_type == {"Combined": "combined", "Calls": "call",
                                                     "Puts": "put"}[side])]
    start, end = c.date_range(df["week_ending"], "weekly_range")
    df = df[(df.week_ending >= start) & (df.week_ending <= end)]
    colours = c.colour_map(c.WEEKLY_CLASS_ORDER)
    order = c.WEEKLY_CLASS_ORDER

    total = df[df.account == "total"][["week_ending", "class", "total_contracts", "premium"]]
    cust = df[df.account == "cust_all"].set_index(["week_ending", "class"])
    small = df[df.account == "cust_1_10"].set_index(["week_ending", "class"])
    ratios = pd.DataFrame({
        # Customers only: OCC books market makers entirely as "closing".
        "customer_opening_share": cust["opening"] / (cust["buys"] + cust["sells"]),
        "customer_open_buy_vs_sell": cust["open_buy_contracts"]
        / (cust["open_buy_contracts"] + cust["open_sell_contracts"]),
        "small_lot_share": (small["buys"] + small["sells"]) / (cust["buys"] + cust["sells"]),
    }).reset_index()

    left, right = st.columns(2)
    with left:
        st.markdown("**Contracts traded per week**")
        fig = c.style(c.lines(total, "week_ending", "total_contracts", "class", order, colours),
                      y_title="contracts")
        c.chart(fig, total, key="weekly_total")
    with right:
        st.markdown("**Premium traded per week**")
        total["premium_bn"] = total["premium"] / 1e9
        fig = c.style(c.lines(total, "week_ending", "premium_bn", "class", order, colours,
                              hover_fmt="$,.1f"), y_title="premium ($ bn)")
        fig.update_yaxes(tickprefix="$", tickformat=",.0f")
        c.chart(fig, total.drop(columns="premium_bn"), key="weekly_premium",
                caption="Premium paid by buyers (opening + closing buys).")

    left, mid, right = st.columns(3)
    with left:
        st.markdown("**Customer opening share**")
        fig = c.style(c.lines(ratios, "week_ending", "customer_opening_share", "class", order,
                              colours, hover_fmt=".1%"), y_title="opening / all", percent=True)
        fig.update_yaxes(rangemode="normal")
        c.chart(fig, ratios[["week_ending", "class", "customer_opening_share"]],
                key="weekly_opening", caption="Customer opening buys + opening sells, as a "
                                              "share of all customer contracts.")
    with mid:
        st.markdown("**Customer opening: buys vs sells**")
        fig = c.style(c.lines(ratios, "week_ending", "customer_open_buy_vs_sell", "class",
                              order, colours, hover_fmt=".1%"),
                      y_title="open buys / all opening", percent=True)
        fig.update_yaxes(rangemode="normal")
        c.chart(fig, ratios[["week_ending", "class", "customer_open_buy_vs_sell"]],
                key="weekly_buy_sell", caption="Above 50%: customers opened more long "
                                               "positions than short ones that week.")
    with right:
        st.markdown("**Small-lot customer share (1–10 contracts)**")
        fig = c.style(c.lines(ratios, "week_ending", "small_lot_share", "class", order,
                              colours, hover_fmt=".1%"),
                      y_title="1–10 lot / all customer", percent=True)
        fig.update_yaxes(rangemode="normal")
        c.chart(fig, ratios[["week_ending", "class", "small_lot_share"]], key="weekly_small",
                caption="Share of customer contracts in trades of 1–10 contracts. A common "
                        "retail proxy: a heuristic, not an account classification.")


c.run_page(render)
