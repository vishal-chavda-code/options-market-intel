"""What each dataset covers: how much of the U.S. options market it sees, and what's missing."""

from __future__ import annotations

import components as c
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Coverage", page_icon=":material/travel_explore:", layout="wide")

OPTION_CLASSES = ("etf", "single_stock", "index", "unclassified_stock")


def span(table: str, col: str) -> str:
    if not c.has_table(table):
        return "not fetched yet"
    row = c.query(f"SELECT CAST(min({col}) AS VARCHAR) AS a, CAST(max({col}) AS VARCHAR) AS b, "
                  f"count(DISTINCT {col}) AS n FROM {c.table_sql(table)}").iloc[0]
    unit = "weeks" if col == "week_ending" else "days"
    return f"{row['a']} to {row['b']} ({row['n']:,} {unit})"


def render() -> None:
    st.title("What's covered")
    st.markdown(
        "**Short answer:** the OCC data is the **whole U.S. listed options market**. OCC clears "
        "every U.S.-listed option on all ~20 U.S. exchanges. Two views are narrower, and the "
        "numbers below show by how much. Nothing here covers options outside the U.S."
    )
    if not c.has_feature("class_exchange_daily"):
        st.info("No data yet. Run `omi backfill`, then `omi features`.")
        return

    day = c.query(f"SELECT max(trade_date) AS d FROM {c.feature_sql('class_daily')}")["d"].iloc[0]
    day = pd.Timestamp(day).date()
    total = c.query(f"""
        SELECT sum(contracts) AS contracts, count(DISTINCT underlying) AS underlyings
        FROM {c.feature_sql('underlying_daily')}
        WHERE trade_date = DATE '{day}' AND product_class IN {OPTION_CLASSES}
    """).iloc[0]

    cboe_share = None
    if c.has_table("cboe_daily_volume"):
        cboe = c.query(f"SELECT total_vol FROM {c.table_sql('cboe_daily_volume')} "
                       f"WHERE trade_date = DATE '{day}' AND category = 'SUM OF ALL PRODUCTS'")
        if not cboe.empty:
            cboe_share = float(cboe["total_vol"].iloc[0]) / total["contracts"]

    cp_share, cp_symbols = None, 0
    if c.has_table("occ_symbol_volume"):
        cp = c.query(f"""
            WITH q AS (SELECT DISTINCT underlying FROM {c.table_sql('occ_symbol_volume')}
                       WHERE trade_date = DATE '{day}')
            SELECT count(DISTINCT u.underlying) AS n, sum(u.contracts) AS contracts
            FROM {c.feature_sql('underlying_daily')} u JOIN q USING (underlying)
            WHERE u.trade_date = DATE '{day}' AND u.product_class IN {OPTION_CLASSES}
        """).iloc[0]
        if cp["n"]:
            cp_symbols = int(cp["n"])
            cp_share = float(cp["contracts"]) / total["contracts"]

    st.subheader(f"On {day:%A %b %d, %Y}")
    cols = st.columns(4)
    cols[0].metric("U.S. options contracts, all exchanges", f"{total['contracts'] / 1e6:,.1f}M",
                   help="From OCC. Matches OCC's published daily total exactly.")
    cols[1].metric("Underlyings with full detail", f"{int(total['underlyings']):,}",
                   help="Every optionable symbol: exchange x expiry x customer/firm/MM.")
    cols[2].metric("Seen by calls-vs-puts per symbol",
                   f"{cp_share:.0%}" if cp_share is not None else "n/a",
                   help=f"{cp_symbols} symbols (your core list + the day's most active) "
                        "carry this share of all U.S. options volume.")
    cols[3].metric("Seen by the Cboe page (OI, put/call)",
                   f"{cboe_share:.0%}" if cboe_share is not None else "n/a",
                   help="One exchange (Cboe Options) out of ~20.")

    left, right = st.columns(2)
    with left:
        st.markdown("**Share of U.S. options volume each dataset sees**")
        rows = [
            ("Daily volume by class & symbol (OCC)", 1.0),
            ("Weekly by class (OCC)", 1.0),
            ("Daily totals, 2 years (OCC)", 1.0),
            ("Calls vs puts per symbol (OCC)", cp_share),
            ("Open interest & put/call (Cboe)", cboe_share),
        ]
        cov = pd.DataFrame([r for r in rows if r[1] is not None], columns=["dataset", "share"])
        cov = cov.iloc[::-1]  # first row on top
        fig = go.Figure(go.Bar(
            y=cov["dataset"], x=cov["share"], orientation="h",
            marker_color=c.colour_map(["x"])["x"], text=cov["share"],
            texttemplate="%{text:.0%}", textposition="outside", cliponaxis=False,
            hovertemplate="%{y}: %{x:.1%}<extra></extra>",
        ))
        c.style(fig, y_title="", percent=True, height=300)
        fig.update_xaxes(tickformat=".0%", range=[0, 1.12])
        fig.update_layout(hovermode="closest", showlegend=False)
        c.chart(fig, cov.iloc[::-1], key="coverage_share",
                caption="Weekly and 2-year totals are all-exchange, but less detailed than the "
                        "daily per-symbol file (see the table below).")
    with right:
        st.markdown("**Where U.S. options trade: the Cboe page is one bar**")
        ex = c.query(f"""
            SELECT exchange, sum(contracts) AS contracts
            FROM {c.feature_sql('class_exchange_daily')}
            WHERE trade_date = DATE '{day}' AND product_class IN {OPTION_CLASSES}
            GROUP BY exchange ORDER BY contracts
        """)
        ex["share"] = ex["contracts"] / ex["contracts"].sum()
        ex["name"] = ex["exchange"].map(c.exchange_names()).fillna(ex["exchange"])
        highlight, muted = c.colour_map(["x"])["x"], c.colour_map(["Other"])["Other"]
        fig = go.Figure(go.Bar(
            y=ex["name"], x=ex["share"], orientation="h",
            marker_color=[highlight if code == "C" else muted for code in ex["exchange"]],
            hovertemplate="%{y}: %{x:.1%}<extra></extra>",
        ))
        c.style(fig, y_title="", percent=True, height=max(300, 20 * len(ex)))
        fig.update_xaxes(tickformat=".0%")
        fig.update_layout(hovermode="closest", showlegend=False)
        c.chart(fig, ex[["name", "contracts", "share"]].iloc[::-1], key="exchange_share",
                caption="Highlighted: CBOE, the only exchange the Cboe statistics page covers "
                        "(its daily total equals OCC's CBOE count to the contract).")

    st.markdown("**Each dataset at a glance**")
    cp_scope = (f"All U.S. exchanges, {cp_symbols} symbols ({cp_share:.0%} of volume)"
                if cp_share is not None else "All U.S. exchanges, chosen symbols only")
    cboe_scope = (f"Cboe Options exchange only ({cboe_share:.0%} of volume)"
                  if cboe_share is not None else "Cboe Options exchange only")
    table = pd.DataFrame([
        {"dataset": "Daily volume by class & symbol (OCC)",
         "covers": "All U.S. exchanges, every symbol",
         "finest detail": "symbol × exchange × expiry × customer / firm / market maker",
         "calls vs puts": "no", "open interest": "no",
         "stored": span("occ_contract_volume", "trade_date"), "page": "Home, Symbols"},
        {"dataset": "Calls vs puts per symbol (OCC)", "covers": cp_scope,
         "finest detail": "symbol × exchange × account type × call/put",
         "calls vs puts": "yes", "open interest": "no",
         "stored": span("occ_symbol_volume", "trade_date"), "page": "Symbols"},
        {"dataset": "Weekly by class (OCC)", "covers": "All U.S. exchanges",
         "finest detail": "class × call/put × opening/closing × premium × trade size",
         "calls vs puts": "yes (weekly)", "open interest": "no",
         "stored": span("occ_weekly_class", "week_ending"), "page": "Weekly by class"},
        {"dataset": "Daily totals (OCC)", "covers": "All U.S. exchanges",
         "finest detail": "equity (incl. ETF) vs index, per day",
         "calls vs puts": "no", "open interest": "no",
         "stored": span("occ_daily_volume", "trade_date"), "page": "Home (bottom)"},
        {"dataset": "Open interest & put/call (Cboe)", "covers": cboe_scope,
         "finest detail": "category (ETF / equity / index / SPX / VIX…) × call/put",
         "calls vs puts": "yes", "open interest": "yes (Cboe only)",
         "stored": span("cboe_daily_volume", "trade_date"), "page": "Cboe open interest"},
    ])
    st.table(table.set_index("dataset"))  # static table: long text wraps instead of scrolling

    left, right = st.columns(2)
    with left:
        st.markdown("**Market-wide numbers not here yet**")
        st.markdown(
            "- **Daily calls vs puts for the whole market:** only weekly so far (Weekly page).\n"
            "- **Open interest for the whole market:** only Cboe's ~19% slice.\n\n"
            "Candidate source: Cboe's *U.S. Options Market Volume Summary* (all exchanges), "
            "listed in the spec but not checked yet."
        )
    with right:
        st.markdown("**Not covered at all**")
        st.markdown(
            "- Options on non-U.S. exchanges (Eurex, Japan, India…)\n"
            "- Options on futures cleared by CME (S&P futures options, Treasury options)\n"
            "- Over-the-counter options"
        )


c.run_page(render)
