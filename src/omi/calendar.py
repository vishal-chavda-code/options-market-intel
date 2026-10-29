"""NYSE trading-session helpers (``XNYS`` from exchange_calendars).

Store timestamps in UTC; display them in America/New_York. Functions that take ``now``
accept a timezone-aware datetime; a naive one is assumed to be US/Eastern wall time.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

ET = ZoneInfo("America/New_York")

# Earliest date any source backfills to (Cboe legacy CSVs, CFTC TFF start in 2006).
CALENDAR_START = "2000-01-01"


@lru_cache(maxsize=1)
def nyse() -> xcals.ExchangeCalendar:
    return xcals.get_calendar("XNYS", start=CALENDAR_START)


def now_et() -> datetime:
    return datetime.now(tz=ET)


def _to_utc(now: datetime | None) -> pd.Timestamp:
    ts = pd.Timestamp(now if now is not None else now_et())
    if ts.tzinfo is None:
        ts = ts.tz_localize(ET)
    return ts.tz_convert("UTC")


def trading_days(start: date, end: date) -> list[date]:
    """NYSE sessions from ``start`` to ``end`` inclusive."""
    return [ts.date() for ts in nyse().sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))]


def last_completed_session(now: datetime | None = None) -> date:
    """Most recent NYSE session whose closing bell has rung by ``now``.

    Respects holidays and early closes (e.g. 13:00 ET the day after Thanksgiving). This is
    the session a nightly job should collect; whether a source has *published* it yet is
    a per-source question.
    """
    cal = nyse()
    now_utc = _to_utc(now)
    session = cal.date_to_session(pd.Timestamp(now_utc.tz_convert(ET).date()), direction="previous")
    if cal.session_close(session) > now_utc:
        session = cal.previous_session(session)
    return session.date()


def previous_session(d: date) -> date:
    """The NYSE session before ``d`` (``d`` itself need not be a session)."""
    cal = nyse()
    ts = pd.Timestamp(d)
    if cal.is_session(ts):
        return cal.previous_session(ts).date()
    return cal.date_to_session(ts, direction="previous").date()


def week_endings(start: date, end: date) -> list[date]:
    """The Friday of each week that has at least one NYSE session, for Fridays in [start, end].

    OCC keys its weekly reports on the calendar Friday **even when the market is closed that
    day** (verified 2026-10-29: Good Friday 2026-04-03 and 2025-07-04 return reports; the
    Thursday before returns "Report is not available.").
    """
    first_monday = start - timedelta(days=start.weekday())
    last_sunday = end + timedelta(days=6 - end.weekday())
    fridays = {d - timedelta(days=d.weekday()) + timedelta(days=4)
               for d in trading_days(first_monday, last_sunday)}
    return sorted(f for f in fridays if start <= f <= end)


def last_week_ending_before(d: date) -> date:
    """The most recent completed week's last session, strictly before ``d``."""
    return [w for w in week_endings(d - timedelta(days=21), d) if w < d][-1]


def is_market_open(now: datetime | None = None) -> bool:
    """True during the NYSE regular session (09:30 to the close, which is excluded)."""
    return bool(nyse().is_open_on_minute(_to_utc(now).floor("min")))


def yyyymmdd(d: date) -> str:
    """Date in the ``yyyymmdd`` form OCC report URLs use."""
    return d.strftime("%Y%m%d")
