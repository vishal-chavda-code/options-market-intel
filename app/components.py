"""Shared dashboard helpers: read-only data access, colours, freshness badges, chart styling.

The app never calls external sources. It reads data/features, data/tables and data/meta
through DuckDB, cached for ``app.refresh_seconds``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from omi import calendar as cal
from omi import pipeline
from omi.config import REFERENCE_DIR, Settings, load_settings

# ---- settings & data access ---------------------------------------------------------------


@st.cache_resource
def settings() -> Settings:
    return load_settings()


def data_dir() -> Path:
    return settings().paths.data_dir


def feature_path(name: str) -> Path:
    return data_dir() / "features" / f"{name}.parquet"


def has_feature(name: str) -> bool:
    return feature_path(name).exists()


def feature_sql(name: str) -> str:
    """A FROM-clause expression for a feature file."""
    return f"read_parquet('{feature_path(name).as_posix()}')"


def has_table(name: str) -> bool:
    return any((data_dir() / "tables" / name).glob("*/data.parquet"))


def table_sql(name: str) -> str:
    glob = (data_dir() / "tables" / name / "*" / "data.parquet").as_posix()
    return f"read_parquet('{glob}', union_by_name = true, hive_partitioning = false)"


@st.cache_data(ttl=300, show_spinner=False)
def query(sql: str) -> pd.DataFrame:
    with duckdb.connect() as con:
        return con.execute(sql).df()


@st.cache_data(ttl=300, show_spinner=False)
def health() -> pd.DataFrame:
    return pipeline.health(settings(), write=False)


@st.cache_data(show_spinner=False)
def exchange_names() -> dict[str, str]:
    df = pd.read_csv(REFERENCE_DIR / "occ_exchanges.csv", dtype=str)
    return dict(zip(df["code"], df["name"], strict=True))


# ---- colours ----------------------------------------------------------------------------
# Validated with the dataviz skill's validate_palette.js (adjacent CVD and normal-vision
# separation pass in both modes). Colour follows the entity: the same class is the same hue
# on every chart. Light-mode slots 3-5 sit below 3:1 on the surface, so every chart has a
# legend and a table view.

_SLOTS = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"],
}
_OTHER = "#898781"
# Expiry buckets are ordered: nearest expiry gets the most contrast against the surface.
_ORDINAL = {
    "light": ["#104281", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef"],
    "dark": ["#b7d3f6", "#6da7ec", "#3987e5", "#256abf", "#184f95"],
}

CLASS_ORDER = ["ETF", "Single stock", "Index, broad", "Index, narrow", "Other"]
ACCOUNT_ORDER = ["Customer", "Firm", "Market maker"]
CP_ORDER = ["Calls", "Puts"]
FAMILY_ORDER = ["S&P 500", "VIX", "Russell 2000", "Nasdaq-100", "Other"]
WEEKLY_CLASS_ORDER = ["ETF", "Single stock", "Index"]


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except AttributeError:
        return "light"


def colour_map(order: list[str]) -> dict[str, str]:
    """Fixed hue per entity in ``order``; 'Other' is always neutral grey."""
    slots = _SLOTS[mode()]
    out, i = {}, 0
    for name in order:
        if name == "Other":
            out[name] = _OTHER
        else:
            out[name] = slots[i]
            i += 1
    return out


def ordinal_map(order: list[str]) -> dict[str, str]:
    ramp = _ORDINAL[mode()]
    return {name: ramp[min(i, len(ramp) - 1)] for i, name in enumerate(order)}


def short(x) -> str:
    """1_234_567 -> '1.2M' for metric tiles."""
    from omi.report import fmt_num

    return fmt_num(x) or "0"


def surface() -> str:
    return "#0e1117" if mode() == "dark" else "#ffffff"


def class_label(product_class: str, index_scope: str | None) -> str:
    """Display bucket for a (product_class, index_scope) pair."""
    if product_class == "etf":
        return "ETF"
    if product_class == "single_stock":
        return "Single stock"
    if product_class == "index":
        return {"broad": "Index, broad", "narrow": "Index, narrow"}.get(index_scope or "", "Other")
    return "Other"


# ---- freshness --------------------------------------------------------------------------

def freshness(sources: list[str]) -> None:
    """One badge per source: green current, orange one period behind, red older or failing."""
    h = health()
    if h.empty:
        st.badge("No data fetched yet", icon=":material/error:", color="red")
        return
    latest = cal.last_completed_session()
    per_row = 3  # wider badges than this get truncated
    cols = [c for i in range(0, len(sources), per_row)
            for c in st.columns(per_row)][: len(sources)]
    for col, source in zip(cols, sources, strict=True):
        rows = h[h["source"] == source]
        with col:
            if rows.empty:
                st.badge(f"{source}: never fetched", icon=":material/error:", color="red")
                continue
            row = rows.iloc[0]
            last_ok = row["last_ok_as_of"]
            failing = row["last_status"] not in ("ok", "not_available")
            if source == "occ_weekly_class":
                expected = cal.last_week_ending_before(latest + pd.Timedelta(days=1))
                behind = 0 if last_ok is not None and last_ok >= expected else 2
            else:
                behind = row["sessions_behind"] if pd.notna(row["sessions_behind"]) else 99
            label = f"{source}: {last_ok or 'none'}"
            if failing or behind >= 2:
                st.badge(f"{label} (stale)" if not failing else f"{label} (last run failed)",
                         icon=":material/error:", color="red")
            elif behind == 1:
                st.badge(f"{label} (1 session behind)", icon=":material/warning:",
                         color="orange")
            else:
                st.badge(f"{label} (current)", icon=":material/check_circle:", color="green")


# ---- charts -----------------------------------------------------------------------------

def _has_date_x(fig: go.Figure) -> bool:
    """True when the traces plot dates along x (daily series, not categories or weeks)."""
    for trace in fig.data:
        xs = getattr(trace, "x", None)
        if xs is not None and len(xs) and getattr(trace, "orientation", None) != "h":
            return isinstance(xs[0], date | pd.Timestamp | np.datetime64)
    return False


def style(fig: go.Figure, *, y_title: str, percent: bool = False, height: int = 340,
          stacked_bars: bool = False) -> go.Figure:
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 28, "b": 8},
        # "normal" keeps the legend in series order (Plotly reverses it for stacked bars).
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.0, "x": 0, "title": None,
                "traceorder": "normal"},
        hovermode="x unified",
        bargap=0.25,
        xaxis_title=None,
        yaxis_title=y_title,
    )
    fig.update_yaxes(tickformat=".0%" if percent else "~s", rangemode="tozero")
    if fig.layout.xaxis.type in (None, "date") and _has_date_x(fig):
        fig.update_xaxes(rangebreaks=[{"bounds": ["sat", "mon"]}])  # no empty weekend columns
    if stacked_bars:
        # 2px surface gap between stacked segments instead of borders.
        fig.update_traces(marker_line_color=surface(), marker_line_width=1)
    else:
        fig.update_traces(selector={"type": "scatter"}, line_width=2)
    return fig


def chart(fig: go.Figure, table: pd.DataFrame, *, key: str, caption: str | None = None) -> None:
    """Plot plus its table-view twin (with CSV download)."""
    st.plotly_chart(fig, width="stretch", theme="streamlit", key=f"fig_{key}")
    if caption:
        st.caption(caption)
    with st.expander("Table view"):
        st.dataframe(table, width="stretch", hide_index=True)
        st.download_button("Download CSV", table.to_csv(index=False).encode(), f"{key}.csv",
                           "text/csv", key=f"dl_{key}")


def bars(df: pd.DataFrame, x: str, y: str, series: str, order: list[str],
         colours: dict[str, str], *, hover_fmt: str = ",.0f") -> go.Figure:
    fig = go.Figure()
    for name in order:
        part = df[df[series] == name]
        if part.empty:
            continue
        fig.add_bar(x=part[x], y=part[y], name=name, marker_color=colours[name],
                    hovertemplate=f"{name}: %{{y:{hover_fmt}}}<extra></extra>")
    fig.update_layout(barmode="stack")
    return fig


def lines(df: pd.DataFrame, x: str, y: str, series: str, order: list[str],
          colours: dict[str, str], *, hover_fmt: str = ",.0f") -> go.Figure:
    fig = go.Figure()
    # Markers help on short series; on long ones they just clutter the line.
    mode = "lines+markers" if df[x].nunique() <= 40 else "lines"
    for name in order:
        part = df[df[series] == name].sort_values(x)
        if part.empty:
            continue
        fig.add_scatter(x=part[x], y=part[y], name=name, mode=mode,
                        line={"color": colours[name], "width": 2},
                        marker={"size": 8, "line": {"color": surface(), "width": 2}},
                        hovertemplate=f"{name}: %{{y:{hover_fmt}}}<extra></extra>")
    return fig


# ---- page scaffolding -------------------------------------------------------------------

def sidebar_refresh() -> int | None:
    """Auto-refresh toggle; returns the fragment interval in seconds (None = paused)."""
    on = st.sidebar.toggle("Auto-refresh", value=True,
                           help="Re-reads stored data every few minutes. The pipeline itself "
                                "updates once a day.")
    return settings().app.refresh_seconds if on else None


def run_page(render: Callable[[], None]) -> None:
    every = sidebar_refresh()
    st.fragment(run_every=every)(render)()


def date_range(dates: pd.Series, key: str) -> tuple[date, date]:
    days = sorted(pd.to_datetime(dates).dt.date.unique())
    if len(days) == 1:
        return days[0], days[0]
    picked = st.sidebar.date_input("Date range", value=(days[0], days[-1]),
                                   min_value=days[0], max_value=days[-1], key=key)
    if isinstance(picked, tuple) and len(picked) == 2:
        return picked
    return days[0], days[-1]
