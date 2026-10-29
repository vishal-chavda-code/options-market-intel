from __future__ import annotations

from datetime import date, datetime

import pytest

from omi.calendar import (
    ET,
    is_market_open,
    last_completed_session,
    last_week_ending_before,
    previous_session,
    trading_days,
    week_endings,
    yyyymmdd,
)


def et(*args: int) -> datetime:
    return datetime(*args, tzinfo=ET)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (et(2026, 10, 29, 15, 59), date(2026, 10, 28)),  # Thu, before the close
        (et(2026, 10, 29, 16, 0), date(2026, 10, 29)),  # closing bell
        (et(2026, 10, 31, 12, 0), date(2026, 10, 30)),  # Saturday -> Friday
        (et(2026, 11, 26, 20, 0), date(2026, 11, 25)),  # Thanksgiving holiday
        (et(2026, 11, 27, 12, 30), date(2026, 11, 25)),  # early-close day, before 13:00
        (et(2026, 11, 27, 13, 30), date(2026, 11, 27)),  # early-close day, after 13:00
        (datetime(2026, 10, 29, 16, 30), date(2026, 10, 29)),  # naive = ET wall time
    ],
)
def test_last_completed_session(now, expected):
    assert last_completed_session(now) == expected


def test_trading_days_skip_holidays():
    assert trading_days(date(2026, 11, 23), date(2026, 11, 27)) == [
        date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25), date(2026, 11, 27),
    ]


def test_previous_session():
    assert previous_session(date(2026, 10, 29)) == date(2026, 10, 28)
    assert previous_session(date(2026, 11, 2)) == date(2026, 10, 30)  # Monday -> Friday
    assert previous_session(date(2026, 11, 1)) == date(2026, 10, 30)  # Sunday -> Friday


@pytest.mark.parametrize(
    ("now", "is_open"),
    [
        (et(2026, 10, 29, 9, 29), False),
        (et(2026, 10, 29, 9, 30), True),
        (et(2026, 10, 29, 15, 59, 30), True),
        (et(2026, 10, 29, 16, 0), False),
        (et(2026, 10, 31, 11, 0), False),  # Saturday
        (et(2026, 11, 27, 13, 30), False),  # after the early close
    ],
)
def test_is_market_open(now, is_open):
    assert is_market_open(now) is is_open


def test_week_endings():
    # OCC keys holiday weeks on the Friday anyway (Good Friday 2026-04-03).
    assert week_endings(date(2026, 3, 30), date(2026, 4, 10)) == [date(2026, 4, 3),
                                                                  date(2026, 4, 10)]
    assert week_endings(date(2025, 6, 30), date(2025, 7, 4)) == [date(2025, 7, 4)]
    assert week_endings(date(2026, 10, 26), date(2026, 10, 28)) == []  # week still running
    assert last_week_ending_before(date(2026, 10, 28)) == date(2026, 10, 23)
    assert last_week_ending_before(date(2026, 10, 30)) == date(2026, 10, 23)
    assert last_week_ending_before(date(2026, 10, 31)) == date(2026, 10, 30)


def test_yyyymmdd():
    assert yyyymmdd(date(2026, 1, 5)) == "20260105"
