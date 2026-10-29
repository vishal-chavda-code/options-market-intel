from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from omi import probe
from omi.config import PROJECT_ROOT, load_settings
from omi.http import FetchResult

AS_OF = date(2026, 10, 28)


@pytest.fixture
def settings():
    return load_settings(PROJECT_ROOT / "config" / "config.example.yaml")


@pytest.mark.parametrize(
    ("rel", "expect", "require", "needle"),
    [
        ("occ/daily_volume_statistics_20261028.csv", "csv", None, "Date,Equity,Index/Others"),
        ("cboe_indices/VIX_History_trimmed.csv", "csv", None, "DATE,OPEN,HIGH,LOW,CLOSE"),
        ("cfe_futures/settlement.csv", "csv", None, "Product,Symbol,Expiration Date,Price"),
        ("treasury/daily_treasury_yield_curve_2026_trimmed.csv", "csv", None, "1 Mo"),
        ("fred/fredgraph_VIXCLS_trimmed.csv", "csv", None, "observation_date,VIXCLS"),
        ("cboe_chains/_VIX_trimmed.json", "json", "data.options", "data.options: 3 items"),
        ("cftc_cot/gpe5-46if_limit1.json", "json", None, "list of 1"),
        ("occ/cont_volume_20261028_trimmed.txt", "text", None, "29 lines"),
        ("occ/volume_query_SPY_20261028.csv", "csv", None, "quantity,underlying,symbol"),
        ("occ/weekly_etf_20261023.csv", "csv", None, "rows"),
        ("symbol_directory/nasdaqlisted_trimmed.txt", "text", None, "Symbol|Security Name"),
    ],
)
def test_real_fixtures_pass_format_check(fixture_bytes, rel, expect, require, needle):
    ok, detail = probe.check_format(expect, fixture_bytes(rel), require)
    assert ok, detail
    assert needle in detail


def test_html_block_page_fails_csv_check():
    page = b"<!DOCTYPE html><html><body>Access denied by corporate policy</body></html>"
    ok, detail = probe.check_format("csv", page)
    assert not ok and "HTML" in detail


def test_html_check_wants_tables():
    assert probe.check_format("html", b"<html><body><table><tr><td>1</td></tr></table>")[0]
    ok, detail = probe.check_format("html", b"<!doctype html><html><body>none</body></html>")
    assert not ok and "no <table>" in detail


def test_json_require_path(fixture_bytes):
    chain = json.loads(fixture_bytes("cboe_chains/_VIX_trimmed.json"))
    chain["data"]["options"] = []
    ok, detail = probe.check_format("json", json.dumps(chain).encode(), "data.options")
    assert not ok and "data.options" in detail


@pytest.mark.parametrize("rel", ["occ/cont_volume_not_available.txt",
                                 "occ/weekly_not_available.txt"])
def test_occ_not_available_messages_fail(fixture_bytes, rel):
    ok, detail = probe.check_format("text", fixture_bytes(rel))
    assert not ok and detail.startswith("source says:")


def test_empty_and_invalid_bodies_fail():
    assert probe.check_format("csv", b"") == (False, "empty body")
    assert not probe.check_format("json", b"{not json")[0]


def test_targets_cover_every_configured_source(settings):
    targets = probe.build_targets(settings, AS_OF)
    assert {t.source for t in targets} == set(settings.sources)
    by_source = {t.source: t for t in targets}
    assert "reportDate=20261028" in by_source["occ_contract_volume"].url
    assert "reportDate=20261023" in by_source["occ_weekly_class"].url  # prior week ending
    assert "dt=2026-10-28" in by_source["cboe_daily"].url


class FakeFetcher:
    """Answers 200 with a canned body for URLs containing a known fragment; DNS failure else.

    ``replies`` maps URL fragment -> (body, final_url or None for no redirect).
    """

    def __init__(self, replies: dict[str, tuple[bytes, str | None]]):
        self.replies = replies
        self.urls: list[str] = []

    def get(self, url: str) -> FetchResult:
        self.urls.append(url)
        for fragment, (body, final_url) in self.replies.items():
            if fragment in url:
                return FetchResult(url=url, status=200, content=body,
                                   headers={"Content-Type": "text/csv"},
                                   final_url=final_url or url, elapsed_s=0.2, attempts=1)
        return FetchResult(url=url, status=None, content=b"", headers={}, final_url=url,
                           elapsed_s=0.1, attempts=2, error_kind="dns", error="getaddrinfo failed")


def test_run_probe_classifies_rows_and_skips(settings, fixture_bytes, monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "TOPSECRET")
    fetcher = FakeFetcher({
        "theocc.com": (fixture_bytes("occ/daily_volume_statistics_20261028.csv"), None),
        "nasdaqtrader.com": (b"<html>blocked</html>", "https://block.corp.test/denied"),
    })
    rows = probe.run_probe(settings, fetcher, AS_OF)
    by_source = {}
    for r in rows:
        by_source.setdefault(r.source, []).append(r)

    assert by_source["occ_daily_volume"][0].result == probe.PASS
    assert by_source["symbol_directory"][0].result == probe.WARN
    assert by_source["symbol_directory"][0].error_kind == "format"
    assert by_source["cftc_cot"][0].result == probe.FAIL
    assert by_source["cftc_cot"][0].error_kind == "dns"
    for disabled in ("finra_regsho", "cboe_indices", "cfe_futures"):
        assert by_source[disabled][0].result == probe.SKIP
    assert not any("finra.org" in u or "VIX_History" in u for u in fetcher.urls)
    # The FRED key never appears in what gets displayed or logged.
    assert all("TOPSECRET" not in r.url and "TOPSECRET" not in r.final_url for r in rows)
    assert probe.exit_code(rows) == 1

    hosts = probe.host_summary(rows)
    assert hosts["marketdata.theocc.com"] == probe.PASS
    assert hosts["block.corp.test"] == probe.WARN
    assert hosts["publicreporting.cftc.gov"] == probe.FAIL


def test_fred_api_skipped_without_key(settings):
    rows = probe.run_probe(settings, FakeFetcher({}), AS_OF, only=["fred"])
    api = next(r for r in rows if "API" in r.target)
    assert api.result == probe.SKIP and "FRED_API_KEY" in api.detail


def test_only_filter_and_include_disabled(settings):
    fetcher = FakeFetcher({})
    rows = probe.run_probe(settings, fetcher, AS_OF, only=["finra_regsho"], include_disabled=True)
    assert [r.source for r in rows] == ["finra_regsho"]
    assert rows[0].result == probe.FAIL
    assert "CNMSshvol20261028.txt" in fetcher.urls[0]


def test_write_log_has_no_payloads(settings, tmp_path, fixture_bytes):
    fetcher = FakeFetcher(
        {"theocc.com": (fixture_bytes("occ/daily_volume_statistics_20261028.csv"), None)}
    )
    rows = probe.run_probe(settings, fetcher, AS_OF, only=["occ_daily_volume"])
    path = probe.write_log(rows, tmp_path)
    with path.open(encoding="utf-8") as fh:
        logged = list(csv.DictReader(fh))
    assert logged[0]["result"] == "PASS" and logged[0]["source"] == "occ_daily_volume"
    assert "50,854,822" not in path.read_text(encoding="utf-8")


def test_all_pass_or_skip_exits_zero():
    rows = [probe.ProbeRow("a", "t", "u", probe.PASS), probe.ProbeRow("b", "t", "u", probe.SKIP)]
    assert probe.exit_code(rows) == 0
