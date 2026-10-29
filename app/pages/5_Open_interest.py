"""Open interest from the nightly option-chain snapshots (Cboe delayed chains, 45 sessions).

Market-wide: biggest open-interest builds and unwinds by underlying, and the largest
contract-level changes. Per symbol: OI by strike for an expiry, change vs the previous
snapshot, OI by expiry, and the OI history over the stored window.
"""

from __future__ import annotations

import components as c
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Open interest", page_icon=":material/stacked_bar_chart:",
                   layout="wide")

CLASS_NAMES = {"etf": "ETF", "single_stock": "Single stock", "index": "Index"}


def _class(row) -> str:
    return c.class_label(row.product_class, row.index_scope) \
        if isinstance(row.product_class, str) else ""


def market_view(day) -> None:
    und = c.query(f"SELECT * FROM {c.feature_sql('chain_underlying_daily')} "
                  f"WHERE as_of = DATE '{day}'")
    if und.empty:
        st.info("No snapshot for this date.")
        return
    und["class"] = [_class(r) for r in und.itertuples()]
    classes = st.multiselect("Classes", ["ETF", "Single stock", "Index, broad", "Index, narrow"],
                             default=["ETF", "Single stock", "Index, broad", "Index, narrow"])
    und = und[und["class"].isin(classes)]
    totals = und[["call_oi", "put_oi", "total_oi", "total_vol", "oi_notional"]].sum()
    m = st.columns(5)
    m[0].metric("Underlyings", f"{len(und):,}")
    m[1].metric("Open interest", c.short(totals.total_oi))
    m[2].metric("Put/call OI", f"{totals.put_oi / max(totals.call_oi, 1):.2f}")
    m[3].metric("Volume / OI", f"{totals.total_vol / max(totals.total_oi, 1):.1%}")
    m[4].metric("OI market value (est.)", "$" + c.short(totals.oi_notional))

    cols = ["underlying", "name", "class", "total_oi", "d_oi", "d_oi_pct", "d_oi_5",
            "pc_oi", "vol_to_oi", "total_vol", "premium_est", "price"]
    fmt = {"total_oi": st.column_config.NumberColumn("Open interest", format="%d"),
           "d_oi": st.column_config.NumberColumn("Change", format="%+d"),
           "d_oi_pct": st.column_config.NumberColumn("Change %", format="percent"),
           "d_oi_5": st.column_config.NumberColumn("5-snapshot change", format="%+d"),
           "pc_oi": st.column_config.NumberColumn("Put/call OI", format="%.2f"),
           "vol_to_oi": st.column_config.NumberColumn("Volume / OI", format="%.2f"),
           "total_vol": st.column_config.NumberColumn("Volume", format="%d"),
           "premium_est": st.column_config.NumberColumn("Premium (est.)", format="dollar"),
           "price": st.column_config.NumberColumn("Underlying", format="%.2f")}
    has_prev = und["d_oi"].notna().any()
    if not has_prev:
        st.caption("Changes appear once two snapshots are stored.")
    left, right = st.columns(2)
    with left:
        st.markdown("**Largest open-interest builds**")
        st.dataframe(und.nlargest(25, "d_oi")[cols] if has_prev else und.nlargest(25, "total_oi")
                     [cols], column_config=fmt, hide_index=True, width="stretch")
    with right:
        st.markdown("**Largest open-interest unwinds**")
        if has_prev:
            st.dataframe(und.nsmallest(25, "d_oi")[cols], column_config=fmt, hide_index=True,
                         width="stretch")
    with st.expander("All underlyings"):
        st.dataframe(und.sort_values("total_oi", ascending=False)[cols], column_config=fmt,
                     hide_index=True, width="stretch")
        st.download_button("Download CSV", und[cols].to_csv(index=False).encode(),
                           f"oi_underlyings_{day}.csv", "text/csv", key="dl_oi_und")

    if c.has_feature("chain_contract_changes"):
        chg = c.query(f"SELECT * FROM {c.feature_sql('chain_contract_changes')} "
                      f"WHERE as_of = DATE '{day}' ORDER BY abs(d_oi) DESC LIMIT 200")
        if not chg.empty:
            st.markdown(f"**Largest contract-level OI changes** (vs {chg.prev_as_of.iloc[0]})")
            st.dataframe(chg[["underlying", "option", "expiry", "strike", "cp", "oi_prev",
                              "oi_now", "d_oi", "volume", "mark", "d_oi_notional", "iv"]],
                         column_config={
                             "d_oi": st.column_config.NumberColumn("Change", format="%+d"),
                             "d_oi_notional": st.column_config.NumberColumn(
                                 "~Notional change", format="dollar"),
                             "iv": st.column_config.NumberColumn("IV", format="percent")},
                         hide_index=True, width="stretch")


def symbol_view(day, days) -> None:
    sym = st.text_input("Symbol", value="SPY",
                        help="OCC-style underlying: SPY, AAPL, SPX, VIX, BRKB").strip().upper()
    if not sym:
        return
    chain = c.query(f"SELECT * FROM {c.table_sql('chain_eod')} "
                    f"WHERE underlying = '{sym}' AND as_of = DATE '{day}'")
    if chain.empty:
        st.info(f"No snapshot of {sym} on {day}. It may not have been in the nightly list, or "
                f"Cboe had no chain for it. Try `omi chain {sym}` for a live look.")
        return
    under = c.query(f"SELECT * FROM {c.table_sql('chain_underlying')} "
                    f"WHERE underlying = '{sym}' AND as_of = DATE '{day}'")
    price = float(under.price.iloc[0]) if not under.empty else None
    calls, puts = chain[chain.cp == "C"], chain[chain.cp == "P"]
    m = st.columns(5)
    m[0].metric("Underlying (close, delayed)", f"{price:,.2f}" if price else "")
    m[1].metric("Call OI", c.short(calls.open_interest.sum()))
    m[2].metric("Put OI", c.short(puts.open_interest.sum()))
    pc = puts.open_interest.sum() / max(calls.open_interest.sum(), 1)
    m[3].metric("Put/call OI", f"{pc:.2f}")
    m[4].metric("Contracts listed", f"{len(chain):,}")

    expiries = sorted(chain.expiry.unique())
    by_exp = chain.groupby("expiry")["open_interest"].sum()
    default = int(pd.Series(by_exp.values).idxmax()) if len(by_exp) else 0
    expiry = st.selectbox("Expiry", expiries, index=default,
                          format_func=lambda e: f"{e}  (OI {c.short(by_exp.get(e, 0))})")
    ex = chain[chain.expiry == expiry]
    prev_day = next((d for d in reversed(days) if d < day), None)
    prev = pd.DataFrame()
    if prev_day is not None:
        prev = c.query(f"SELECT option, open_interest AS oi_prev FROM {c.table_sql('chain_eod')} "
                       f"WHERE underlying = '{sym}' AND as_of = DATE '{prev_day}' "
                       f"AND expiry = DATE '{expiry}'")
    ex = ex.merge(prev, on="option", how="left") if not prev.empty else ex.assign(oi_prev=None)
    ex["d_oi"] = ex["open_interest"] - ex["oi_prev"]
    colours = c.colour_map(c.CP_ORDER)

    left, right = st.columns(2)
    with left:
        st.markdown(f"**Open interest by strike, {expiry}**")
        fig = go.Figure()
        for cp, label in (("C", "Calls"), ("P", "Puts")):
            part = ex[ex.cp == cp].sort_values("strike")
            fig.add_bar(x=part.strike, y=part.open_interest, name=label,
                        marker_color=colours[label],
                        hovertemplate=f"{label} %{{x}}: %{{y:,}}<extra></extra>")
        if price:
            fig.add_vline(x=price, line_dash="dot", annotation_text="spot")
        fig.update_layout(barmode="group")
        c.chart(c.style(fig, y_title="open contracts"),
                ex[["option", "strike", "cp", "open_interest", "volume", "bid", "ask", "iv",
                    "delta"]].sort_values(["strike", "cp"]), key=f"oi_strike_{sym}")
    with right:
        st.markdown(f"**OI change by strike vs {prev_day or 'previous snapshot'}**")
        if prev.empty:
            st.caption("Needs a previous snapshot.")
        else:
            fig = go.Figure()
            for cp, label in (("C", "Calls"), ("P", "Puts")):
                part = ex[ex.cp == cp].sort_values("strike")
                fig.add_bar(x=part.strike, y=part.d_oi, name=label, marker_color=colours[label],
                            hovertemplate=f"{label} %{{x}}: %{{y:+,}}<extra></extra>")
            if price:
                fig.add_vline(x=price, line_dash="dot")
            fig.update_layout(barmode="group")
            fig = c.style(fig, y_title="change in open contracts")
            fig.update_yaxes(rangemode="normal")
            c.chart(fig, ex[["option", "strike", "cp", "oi_prev", "open_interest", "d_oi"]]
                    .sort_values(["strike", "cp"]), key=f"oi_chg_{sym}")

    left, right = st.columns(2)
    with left:
        st.markdown("**Open interest by expiry**")
        e = chain.groupby(["expiry", "cp"], as_index=False)["open_interest"].sum()
        e["side"] = e.cp.map({"C": "Calls", "P": "Puts"})
        fig = c.style(c.bars(e, "expiry", "open_interest", "side", c.CP_ORDER, colours),
                      y_title="open contracts", stacked_bars=True)
        fig.update_xaxes(type="category")
        c.chart(fig, e[["expiry", "side", "open_interest"]], key=f"oi_exp_{sym}")
    with right:
        st.markdown(f"**{sym} open interest over the stored window**")
        hist = c.query(f"SELECT as_of, call_oi, put_oi FROM "
                       f"{c.feature_sql('chain_underlying_daily')} WHERE underlying = '{sym}' "
                       "ORDER BY as_of")
        long = hist.melt("as_of", var_name="side", value_name="oi")
        long["side"] = long.side.map({"call_oi": "Calls", "put_oi": "Puts"})
        long["as_of"] = pd.to_datetime(long.as_of).dt.date
        fig = c.style(c.lines(long, "as_of", "oi", "side", c.CP_ORDER, colours),
                      y_title="open contracts")
        c.chart(fig, hist, key=f"oi_hist_{sym}")


def render() -> None:
    st.title("Open interest (option chains)")
    st.caption("Nightly snapshot of every listed contract from Cboe's delayed chains (an "
               "undocumented endpoint). Open interest is OCC's start-of-session figure; quotes "
               "are ~15 min delayed closes. A rolling window of sessions is kept "
               "(`retention_sessions`, default 45).")
    c.freshness(["cboe_chains"])
    if not c.has_feature("chain_underlying_daily"):
        st.info("No chain snapshots yet. They are taken by `omi daily` on the session's own "
                "evening (after 16:15 ET).")
        return
    days = sorted(pd.to_datetime(c.query(
        f"SELECT DISTINCT as_of FROM {c.feature_sql('chain_underlying_daily')}")["as_of"])
        .dt.date)
    day = st.sidebar.selectbox("Snapshot", list(reversed(days)), key="oi_day")
    market, symbol = st.tabs(["Market-wide", "By symbol"])
    with market:
        market_view(day)
    with symbol:
        symbol_view(day, days)


c.run_page(render)
