from __future__ import annotations

import pytest
import requests

from omi.config import NetworkConfig
from omi.http import HttpClient, RateLimiter, classify_error, redact_url, short_error


def make_response(status: int, body: bytes = b"", headers: dict | None = None,
                  url: str = "https://example.test/a") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.headers.update(headers or {})
    resp.url = url
    resp.reason = "test"
    return resp


class FakeSession:
    """Plays back a script of responses / exceptions, recording each call's kwargs."""

    def __init__(self, *script):
        self.script = list(script)
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def client_with(*script, net: NetworkConfig | None = None, attempts: int = 4):
    sleeps: list[float] = []
    session = FakeSession(*script)
    net = net or NetworkConfig(rate_limits={"default_rps": 1000.0})
    client = HttpClient(net, max_attempts=attempts, session=session, sleep=sleeps.append)
    return client, session, sleeps


def test_retries_5xx_then_succeeds():
    client, session, sleeps = client_with(make_response(503), make_response(200, b"ok"))
    res = client.get("https://example.test/a")
    assert res.ok and res.content == b"ok"
    assert res.attempts == 2
    assert len(session.calls) == 2
    assert any(s >= 2 for s in sleeps)  # exponential backoff starts at 2 s


def test_honours_retry_after():
    client, _, sleeps = client_with(
        make_response(429, headers={"Retry-After": "7"}), make_response(200, b"ok")
    )
    assert client.get("https://example.test/a").ok
    assert 7.0 in sleeps


def test_gives_up_after_max_attempts_and_returns_last_response():
    client, session, _ = client_with(*[make_response(503)] * 3, attempts=3)
    res = client.get("https://example.test/a")
    assert res.status == 503 and not res.ok
    assert res.attempts == 3 and len(session.calls) == 3
    assert res.error_kind == "http"


def test_404_is_not_retried():
    client, session, _ = client_with(make_response(404))
    res = client.get("https://example.test/a")
    assert res.status == 404 and res.attempts == 1 and len(session.calls) == 1


def test_connection_errors_never_raise():
    err = requests.exceptions.ConnectionError("connection reset")
    client, _, _ = client_with(err, err, attempts=2)
    res = client.get("https://example.test/a")
    assert res.status is None and not res.ok
    assert res.error_kind == "connection" and res.attempts == 2
    assert "ConnectionError" in res.error


def test_ssl_errors_are_not_retried():
    client, session, _ = client_with(requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"))
    res = client.get("https://example.test/a")
    assert res.error_kind == "ssl" and res.attempts == 1 and len(session.calls) == 1


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (requests.exceptions.ConnectionError("NameResolutionError: getaddrinfo failed"), "dns"),
        (requests.exceptions.ProxyError("407 Proxy Authentication Required"), "proxy"),
        (requests.exceptions.ConnectTimeout("timed out"), "timeout"),
        (requests.exceptions.ReadTimeout("timed out"), "timeout"),
        (requests.exceptions.SSLError("bad cert"), "ssl"),
        (ValueError("boom"), "other"),
    ],
)
def test_classify_error(exc, kind):
    assert classify_error(exc) == kind


def test_proxy_ca_bundle_and_user_agent_are_applied(tmp_path):
    pem = tmp_path / "corp.pem"
    pem.write_text("dummy", encoding="utf-8")
    net = NetworkConfig(https_proxy="http://proxy.corp:8080", ca_bundle=pem,
                        user_agent_contact="desk@example.org", rate_limits={"default_rps": 1000})
    client, session, _ = client_with(make_response(200), net=net)
    client.get("https://example.test/a")
    _, kwargs = session.calls[0]
    assert kwargs["proxies"] == {"https": "http://proxy.corp:8080", "http": "http://proxy.corp:8080"}
    assert kwargs["verify"] == str(pem)
    assert kwargs["timeout"] == (10.0, 60.0)
    assert "contact: desk@example.org" in session.headers["User-Agent"]


def test_secrets_are_redacted_from_results():
    client, _, _ = client_with(make_response(200, url="https://api.test/x?api_key=SECRET&id=1"))
    res = client.get("https://api.test/x?api_key=SECRET&id=1")
    assert "SECRET" not in res.url and "SECRET" not in res.final_url


def test_short_error_keeps_root_cause():
    exc = requests.exceptions.SSLError(
        "HTTPSConnectionPool(host='h', port=443): Max retries exceeded with url: /x "
        "(Caused by SSLError(SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED')))"
    )
    assert short_error(exc) == (
        "SSLError: SSLError(SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED'))"
    )
    assert short_error(ValueError("plain")) == "ValueError: plain"


def test_redact_url():
    assert redact_url("https://h/x?series_id=VIX&api_key=abc") == "https://h/x?series_id=VIX&api_key=***"
    assert redact_url("http://bob:pw@proxy:8080") == "http://bob:***@proxy:8080"
    assert redact_url("https://h/r.json?$limit=1") == "https://h/r.json?$limit=1"
    treasury = "https://t/x.csv/2026/all?type=y&field_tdr_date_value=2026&page&_format=csv"
    assert redact_url(treasury) == treasury  # bare "&page&" must survive untouched
    assert redact_url("https://h/x?monkey=1") == "https://h/x?monkey=1"  # 'key' only as a name


def test_rate_limiter_spaces_requests_per_host():
    now = [0.0]
    waits: list[float] = []
    limiter = RateLimiter(0.5, clock=lambda: now[0], sleep=waits.append)
    limiter.acquire()  # first request goes straight through
    limiter.acquire()  # second must wait 1 / 0.5 = 2 s
    assert waits == [2.0]
    now[0] = 100.0  # long idle: no wait
    limiter.acquire()
    assert waits == [2.0]
