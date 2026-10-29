from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from omi.config import PROJECT_ROOT, load_settings
from omi.features.product_class import bucket_sql, build
from omi.quality import conform
from omi.sources.occ_contract_volume import CONTRACT_VOLUME
from omi.sources.symbol_directory import LISTED
from omi.storage import Storage

D = date(2026, 10, 28)
NOW = datetime(2026, 10, 29, tzinfo=UTC)


def cv(underlying, kind, expiry, cust, firm, mm, exchange="C", symbol=None):
    return {"trade_date": D, "underlying": underlying, "symbol": symbol or underlying,
            "exchange": exchange, "product_kind": kind, "expiry": expiry,
            "cust_sides": cust, "firm_sides": firm, "mm_sides": mm}


@pytest.fixture
def built(tmp_path):
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    storage = Storage(tmp_path)
    rows = [
        cv("SPY", "OSTK", D, 60, 0, 40),  # 0DTE
        cv("SPY", "OSTK", date(2026, 11, 4), 10, 0, 10, exchange="X"),  # 7 days
        cv("AAPL", "OSTK", date(2026, 11, 20), 30, 10, 20),  # 23 days
        cv("SPX", "OIND", date(2027, 1, 15), 100, 20, 80, symbol="SPXW"),  # 79 days
        cv("VIX", "OIND", date(2027, 6, 18), 8, 2, 10),  # 233 days
        cv("XAU", "OIND", date(2026, 11, 20), 1, 0, 1),
        cv("NEWIX", "OIND", date(2026, 11, 20), 1, 0, 1),  # not in the reference list
        cv("ZZZZQ", "OSTK", date(2026, 11, 20), 2, 0, 2),  # not in the symbol directory
        cv("VIX", "OCHF", date(2026, 11, 18), 50, 0, 0, exchange="F", symbol="VX"),
    ]
    storage.upsert(CONTRACT_VOLUME, conform(CONTRACT_VOLUME, pd.DataFrame(rows), NOW))
    listings = pd.DataFrame([
        {"as_of": D, "listing_file": "otherlisted", "symbol": "SPY", "occ_symbol": "SPY",
         "name": "SPDR S&P 500", "listing_exchange": "P", "is_etf": True, "is_test_issue": False},
        {"as_of": D, "listing_file": "nasdaqlisted", "symbol": "AAPL", "occ_symbol": "AAPL",
         "name": "Apple", "listing_exchange": "Q", "is_etf": False, "is_test_issue": False},
    ])
    storage.upsert(LISTED, conform(LISTED, listings, NOW))
    counts = build(storage, settings)
    return storage, counts


def test_classification(built):
    storage, counts = built
    cls = pd.read_parquet(storage.features_dir / "underlying_class.parquet")
    cls = cls.set_index(["underlying", "product_kind"])
    assert cls.loc[("SPY", "OSTK"), "product_class"] == "etf"
    assert cls.loc[("AAPL", "OSTK"), "product_class"] == "single_stock"
    assert cls.loc[("ZZZZQ", "OSTK"), "product_class"] == "unclassified_stock"
    spx = cls.loc[("SPX", "OIND")]
    assert (spx.product_class, spx.index_scope, spx.index_family) == ("index", "broad", "S&P 500")
    vix = cls.loc[("VIX", "OIND")]
    assert (vix.product_class, vix.index_scope, vix.index_family) == ("index", "broad", "VIX")
    assert cls.loc[("XAU", "OIND"), "index_scope"] == "narrow"
    assert cls.loc[("NEWIX", "OIND"), "index_scope"] == "unclassified"
    assert cls.loc[("VIX", "OCHF"), "product_class"] == "futures"
    assert pd.isna(cls.loc[("SPY", "OSTK"), "index_scope"])


def test_expiry_buckets_and_contracts(built):
    storage, _ = built
    ud = pd.read_parquet(storage.features_dir / "underlying_daily.parquet")
    buckets = ud.set_index(["underlying", "expiry_bucket"])["contracts"]
    assert buckets[("SPY", "0DTE")] == 50  # (60 + 0 + 40) sides / 2
    assert buckets[("SPY", "1-7d")] == 10
    assert buckets[("AAPL", "8-30d")] == 30
    assert buckets[("SPX", "31-90d")] == 100
    assert buckets[("VIX", "91d+")] == 10


def test_class_daily_totals(built):
    storage, _ = built
    cd = pd.read_parquet(storage.features_dir / "class_daily.parquet")
    etf = cd[cd.product_class == "etf"].iloc[0]
    assert etf.contracts == 60 and etf.cust_sides == 70 and etf.underlyings == 1
    idx = cd[cd.product_class == "index"].set_index("index_family")["contracts"]
    assert idx["VIX"] == 10 and idx["S&P 500"] == 100
    ex = pd.read_parquet(storage.features_dir / "class_exchange_daily.parquet")
    assert set(ex[ex.product_class == "etf"]["exchange"]) == {"C", "X"}


def test_bucket_sql_labels():
    label, order = bucket_sql([0, 7, 30, 90])
    for text in ("'0DTE'", "'1-7d'", "'8-30d'", "'31-90d'", "'91d+'"):
        assert text in label
    assert "ELSE 4" in order


def test_build_without_data_returns_nothing(tmp_path):
    settings = load_settings(PROJECT_ROOT / "config" / "config.example.yaml")
    assert build(Storage(tmp_path), settings) == {}
