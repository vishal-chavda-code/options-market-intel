from __future__ import annotations

from datetime import date

import pytest

from omi.quality import conform
from omi.sources.base import FetchRequest, NotAvailable
from omi.sources.cboe_daily_stats import PC_RATIOS, VOLUME_OI, CboeDailyStats
from omi.sources.symbol_directory import LISTED, SymbolDirectory, occ_symbol

D = date(2026, 10, 28)


def test_cboe_daily_tables(fixture_bytes):
    out = CboeDailyStats().parse(FetchRequest("cboe_daily", "r", D, "u"),
                                 fixture_bytes("cboe_daily/daily_2026-10-28_tables.html"))
    vol = conform(VOLUME_OI, out["cboe_daily_volume"]).set_index("category")
    ratios = conform(PC_RATIOS, out["cboe_pc_ratios"]).set_index("ratio_name")["value"]
    assert list(vol.index) == ["SUM OF ALL PRODUCTS", "INDEX OPTIONS", "EXCHANGE TRADED PRODUCTS",
                               "EQUITY OPTIONS", "CBOE VOLATILITY INDEX (VIX)", "SPX + SPXW", "OEX"]
    total = vol.loc["SUM OF ALL PRODUCTS"]
    assert (total.call_vol, total.put_vol, total.total_vol) == (5_582_664, 5_037_851, 10_620_515)
    assert (total.call_oi, total.put_oi, total.total_oi) == (383_100_099, 288_174_931, 671_275_030)
    assert vol.loc["EXCHANGE TRADED PRODUCTS", "total_vol"] == 2_402_238
    assert ratios["TOTAL PUT/CALL RATIO"] == pytest.approx(0.90)
    assert ratios["EQUITY PUT/CALL RATIO"] == pytest.approx(0.54)


def test_cboe_daily_page_without_tables_is_not_available():
    with pytest.raises(NotAvailable):
        CboeDailyStats().parse(FetchRequest("cboe_daily", "r", D, "u"),
                               b"<!DOCTYPE html><html><body>loading</body></html>")


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("nasdaqlisted", {"AAAP": True, "AAPL": False, "QQQ": True, "ZJZZT": False}),
        ("otherlisted", {"A": False, "BRK.B": False, "IWM": True, "SPY": True}),
    ],
)
def test_symbol_directory(fixture_bytes, key, expected):
    out = SymbolDirectory().parse(FetchRequest("symbol_directory", "listings", D, "u", key=key),
                                  fixture_bytes(f"symbol_directory/{key}_trimmed.txt"))
    df = conform(LISTED, out["ref_listed_symbols"]).set_index("symbol")
    assert df["is_etf"].to_dict() == expected
    if key == "otherlisted":
        assert df.loc["BRK.B", "occ_symbol"] == "BRKB"
        assert df.loc["SPY", "listing_exchange"] == "P"
    else:
        assert bool(df.loc["ZJZZT", "is_test_issue"]) is True
        assert set(df["listing_exchange"]) == {"Q"}


def test_occ_symbol_normalisation():
    assert occ_symbol("BRK.B") == "BRKB"
    assert occ_symbol("abc$p") == "ABCP"
