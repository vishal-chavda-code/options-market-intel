from __future__ import annotations

from datetime import date

import pytest

from omi.config import PROJECT_ROOT, load_settings
from omi.quality import conform
from omi.sources.base import FetchRequest, NotAvailable, RunContext, SchemaDrift
from omi.sources.occ_contract_volume import CONTRACT_VOLUME, OccContractVolume
from omi.sources.occ_daily_volume import DAILY, DAILY_FUTURES, OccDailyVolume
from omi.sources.occ_symbol_volume import SYMBOL_VOLUME, OccSymbolVolume, top_underlyings
from omi.sources.occ_weekly_class import EXCHANGE_PREMIUM, WEEKLY, OccWeeklyClass

D = date(2026, 10, 28)


def req(source: str, as_of: date = D, key: str = "") -> FetchRequest:
    return FetchRequest(source, "r", as_of, "https://x.test", key=key)


# ---- contract volume -------------------------------------------------------------------
def test_contract_volume_parses_fixed_width(fixture_bytes):
    out = OccContractVolume().parse(req("occ_contract_volume"),
                                    fixture_bytes("occ/cont_volume_20261028_trimmed.txt"))
    df = conform(CONTRACT_VOLUME, out["occ_contract_volume"])
    assert len(df) == 28
    first = df.iloc[0]  # "A     A     A000000028000000000000000070OSTK20261120"
    assert (first.underlying, first.symbol, first.exchange, first.product_kind) == \
        ("A", "A", "A", "OSTK")
    assert (first.cust_sides, first.firm_sides, first.mm_sides) == (28, 0, 70)
    assert first.expiry == date(2026, 11, 20)
    assert df["product_kind"].value_counts().to_dict() == {"OSTK": 14, "OIND": 10,
                                                           "OCHF": 2, "OOOF": 2}
    assert set(df.loc[df.underlying == "SPX", "symbol"]) <= {"SPX", "SPXW", "2SPX", "4SPX"}


def test_contract_volume_not_published(fixture_bytes):
    with pytest.raises(NotAvailable):
        OccContractVolume().parse(req("occ_contract_volume"),
                                  fixture_bytes("occ/cont_volume_not_available.txt"))


def test_contract_volume_wrong_date_is_drift(fixture_bytes):
    with pytest.raises(SchemaDrift, match="requested"):
        OccContractVolume().parse(req("occ_contract_volume", date(2026, 10, 27)),
                                  fixture_bytes("occ/cont_volume_20261028_trimmed.txt"))


def test_contract_volume_layout_change_is_drift(fixture_bytes):
    raw = fixture_bytes("occ/cont_volume_20261028_trimmed.txt")
    raw = raw.replace(b"OSTK2026", b"OSTKX2026", 1)
    with pytest.raises(SchemaDrift, match="52 chars"):
        OccContractVolume().parse(req("occ_contract_volume"), raw)


# ---- symbol volume (volume query) ------------------------------------------------------
def test_symbol_volume_parses_spy(fixture_bytes):
    out = OccSymbolVolume().parse(req("occ_symbol_volume", key="SPY"),
                                  fixture_bytes("occ/volume_query_SPY_20261028.csv"))
    df = conform(SYMBOL_VOLUME, out["occ_symbol_volume"])
    assert len(df) == 133
    assert df["sides"].sum() == 15_872_574  # all expiries, all accounts, both sides
    assert set(df["account_type"]) == {"C", "F", "M"}
    assert set(df["cp"]) == {"C", "P"}
    assert set(df["underlying"]) == {"SPY"}
    assert {"SPY", "2SPY"} <= set(df["symbol"])


def test_symbol_volume_message_and_empty_are_not_available():
    src = OccSymbolVolume()
    with pytest.raises(NotAvailable, match="Symbol is required"):
        src.parse(req("occ_symbol_volume", key="SPY"), b"Symbol is required.")
    header_only = b"quantity,underlying,symbol,actype,porc,exchange,actdate\r\n"
    with pytest.raises(NotAvailable, match="no volume rows"):
        src.parse(req("occ_symbol_volume", key="SPY"), header_only)


def test_symbol_volume_plan_adds_top_n(fixture_bytes):
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    latest = OccContractVolume().parse(req("occ_contract_volume"),
                                       fixture_bytes("occ/cont_volume_20261028_trimmed.txt"))
    ctx = RunContext(settings, D, read_latest=lambda name: latest["occ_contract_volume"])
    syms = OccSymbolVolume().symbols(ctx)
    assert syms[:5] == ["SPX", "SPY", "QQQ", "IWM", "VIX"]  # universe first, in order
    assert "A" in syms  # from the top-N of the latest contract-volume day
    assert "VX" not in syms  # options on futures are not underlyings we query
    cv = latest["occ_contract_volume"]
    opts = cv[cv.product_kind.isin(["OSTK", "OIND"])]
    expected = (opts.cust_sides + opts.firm_sides + opts.mm_sides).groupby(opts.underlying).sum()
    assert top_underlyings(cv, 2) == list(expected.nlargest(2).index)


# ---- weekly by class -------------------------------------------------------------------
def test_weekly_etf_report(fixture_bytes):
    out = OccWeeklyClass().parse(req("occ_weekly_class", date(2026, 10, 23), "etf"),
                                 fixture_bytes("occ/weekly_etf_20261023.csv"))
    vol = conform(WEEKLY, out["occ_weekly_class"]).set_index(
        ["flex", "option_type", "account"])
    prem = conform(EXCHANGE_PREMIUM, out["occ_weekly_class_exchange"])
    assert len(vol) == 2 * 3 * 7  # standard + FLEX blocks x call/put/combined x 7 rows
    row = vol.loc[(False, "call", "cust_1_10")]
    assert row.total_contracts == 11_594_170
    assert row.open_buy_contracts == 10_320_725
    assert row.open_buy_premium == pytest.approx(2_476_452_758.0)
    assert vol.loc[(False, "call", "mm"), "open_buy_contracts"] == 0  # MMs only in "close"
    total = vol.loc[(False, "combined", "total")]
    buys = total.open_buy_contracts + total.close_buy_contracts
    sells = total.open_sell_contracts + total.close_sell_contracts
    assert total.total_contracts == buys == sells == 156_197_211
    assert len(prem) == 20 + 6  # exchanges in the standard and FLEX blocks
    amex = prem[(prem.exchange == "AMEX") & ~prem.flex].iloc[0]
    assert amex.call_premium == pytest.approx(1_679_842_982.0)


def test_weekly_index_report(fixture_bytes):
    out = OccWeeklyClass().parse(req("occ_weekly_class", date(2026, 10, 23), "index"),
                                 fixture_bytes("occ/weekly_index_20261023.csv"))
    vol = conform(WEEKLY, out["occ_weekly_class"]).set_index(["flex", "option_type", "account"])
    assert vol.loc[(False, "call", "cust_1_10"), "total_contracts"] == 4_055_580
    assert set(vol.reset_index()["product_class"]) == {"index"}


def test_weekly_not_available_and_wrong_week(fixture_bytes):
    src = OccWeeklyClass()
    with pytest.raises(NotAvailable):
        src.parse(req("occ_weekly_class", date(2026, 10, 23), "etf"),
                  fixture_bytes("occ/weekly_not_available.txt"))
    with pytest.raises(SchemaDrift, match="week ending"):
        src.parse(req("occ_weekly_class", date(2026, 10, 16), "etf"),
                  fixture_bytes("occ/weekly_etf_20261023.csv"))


def test_weekly_plan_uses_week_endings():
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    plan = OccWeeklyClass().plan([date(2026, 3, 30), date(2026, 4, 9)], RunContext(settings, D))
    # Good Friday 2026-04-03 (market closed): OCC still keys the week on that Friday.
    assert sorted({r.as_of for r in plan}) == [date(2026, 4, 3)]
    assert sorted(r.key for r in plan) == ["equity", "etf", "index"]


# ---- daily volume statistics -----------------------------------------------------------
def test_daily_volume_month_to_date(fixture_bytes):
    out = OccDailyVolume().parse(req("occ_daily_volume"),
                                 fixture_bytes("occ/daily_volume_statistics_20261028.csv"))
    daily = conform(DAILY, out["occ_daily_volume"]).set_index("trade_date")
    fut = conform(DAILY_FUTURES, out["occ_daily_futures_volume"])
    assert len(daily) == 20 and len(fut) == 20  # 1st..28th Oct: 20 sessions, totals skipped
    assert daily.loc[D, "equity"] == 50_854_822
    assert daily.loc[D, "index_other"] == 5_025_345
    assert daily.loc[D, "occ_total"] == 56_061_448


def test_daily_volume_not_yet_through_requested_date(fixture_bytes):
    with pytest.raises(NotAvailable, match="before 2026-10-29"):
        OccDailyVolume().parse(req("occ_daily_volume", date(2026, 10, 29)),
                               fixture_bytes("occ/daily_volume_statistics_20261028.csv"))


def test_daily_volume_file_past_requested_date_is_fine(fixture_bytes):
    # OCC answers an early-month reportDate with the month through its latest day.
    out = OccDailyVolume().parse(req("occ_daily_volume", date(2026, 10, 1)),
                                 fixture_bytes("occ/daily_volume_statistics_20261028.csv"))
    assert len(out["occ_daily_volume"]) == 20


def test_daily_volume_plan_one_request_per_month():
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    dates = [date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)]
    plan = OccDailyVolume().plan(dates, RunContext(settings, D))
    # September is finished: one file. October is in progress: one per session, so an
    # unpublished latest day doesn't hold back the earlier ones.
    assert [r.as_of for r in plan] == [date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)]
