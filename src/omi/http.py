"""The only module that makes network requests.

Everything goes through ``HttpClient.get``, which handles:
  * proxies from config (falling back to HTTPS_PROXY / HTTP_PROXY / NO_PROXY env vars),
  * a custom CA bundle or the OS trust store, for networks with SSL inspection,
  * an identifying User-Agent,
  * a per-host rate limit,
  * retries with exponential backoff and jitter on 429 / 5xx / connection errors,
    honouring ``Retry-After``,
and never raises: failures come back as a ``FetchResult`` with ``error_kind`` set, so one
broken source cannot take down a run.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import requests
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    retry_if_result,
    stop_after_attempt,
)

from omi import __version__
from omi.config import NetworkConfig

log = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 120.0
_SECRET_PARAM = re.compile(r"(?i)([?&](?:api_key|apikey|key|token|access_token)=)[^&#]*")


@dataclass
class FetchResult:
    """Outcome of one logical GET (after any retries)."""

    url: str  # as requested, with secrets redacted
    status: int | None  # HTTP status of the last attempt; None if no response arrived
    content: bytes
    headers: dict[str, str]
    final_url: str  # after redirects, redacted
    elapsed_s: float
    attempts: int
    error_kind: str | None = None  # dns | ssl | proxy | timeout | connection | other
    error: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "")


class RateLimiter:
    """Per-host spacing: at most ``rate`` requests per second to one host.

    This is a token bucket with a capacity of one token, i.e. no bursts.
    """

    def __init__(
        self,
        rate: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        self.interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next_free = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._next_free - now
            self._next_free = max(now, self._next_free) + self.interval
        if wait > 0:
            self._sleep(wait)


def redact_url(url: str) -> str:
    """Hide API keys in the query string and passwords in proxy URLs before logging.

    Everything else is left byte-for-byte intact (e.g. Treasury's bare ``&page&``).
    """
    url = _SECRET_PARAM.sub(r"\1***", url)
    parts = urlsplit(url)
    if "@" in parts.netloc:
        userinfo, host = parts.netloc.rsplit("@", 1)
        if ":" in userinfo:
            user = userinfo.split(":", 1)[0]
            return urlunsplit(parts._replace(netloc=f"{user}:***@{host}"))
    return url


def classify_error(exc: BaseException) -> str:
    """Map a requests exception to a short label that points at the likely network cause."""
    if isinstance(exc, requests.exceptions.SSLError):
        return "ssl"  # usually SSL inspection: configure ca_bundle or use_os_truststore
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy"
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = repr(exc)
        dns_markers = ("NameResolutionError", "getaddrinfo failed", "Name or service not known",
                       "nodename nor servname", "Temporary failure in name resolution")
        return "dns" if any(m in text for m in dns_markers) else "connection"
    return "other"


def short_error(exc: BaseException) -> str:
    """Drop urllib3's "Max retries exceeded with url: ..." wrapper and keep the root cause."""
    text = str(exc)
    match = re.search(r"\(Caused by (.*)\)\s*$", text, flags=re.S)
    return f"{type(exc).__name__}: {match.group(1) if match else text}"


def _is_transient(exc: BaseException) -> bool:
    # SSL failures are deterministic (wrong trust store), so retrying only wastes time.
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    return isinstance(
        exc,
        requests.exceptions.ConnectionError
        | requests.exceptions.Timeout
        | requests.exceptions.ChunkedEncodingError,
    )


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a Retry-After header: either delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def user_agent(net: NetworkConfig) -> str:
    return f"omi/{__version__} (internal research; contact: {net.user_agent_contact})"


def describe_proxy(net: NetworkConfig) -> str:
    """Where proxy settings come from, for diagnostics (credentials redacted)."""
    if net.https_proxy or net.http_proxy:
        return f"config: {redact_url(net.https_proxy or net.http_proxy or '')}"
    # Env vars, or on Windows the system proxy from Internet Options (PAC scripts are not read).
    env = requests.utils.getproxies()
    if env:
        shown = ", ".join(f"{k}={redact_url(v)}" for k, v in sorted(env.items()))
        return f"env/OS settings: {shown}"
    return "none (direct connection)"


def describe_trust(net: NetworkConfig) -> str:
    if net.ca_bundle:
        return f"CA bundle {net.ca_bundle}"
    if net.use_os_truststore:
        return "OS certificate store (truststore)"
    return "certifi default bundle"


class HttpClient:
    """Shared HTTP client. Create one per run and reuse it so rate limits apply across sources."""

    def __init__(
        self,
        net: NetworkConfig,
        *,
        max_attempts: int | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._net = net
        self._max_attempts = max_attempts or net.max_attempts
        self._sleep = sleep
        self._timeout = (net.connect_timeout_s, net.read_timeout_s)
        self._limiters: dict[str, RateLimiter] = {}
        self._limiters_lock = threading.Lock()

        if net.use_os_truststore and net.ca_bundle is None:
            import truststore

            truststore.inject_into_ssl()
        elif net.use_os_truststore:
            log.warning("Both ca_bundle and use_os_truststore are set; using ca_bundle only")
        # Passed on every request (not set on the session) so they beat env-var defaults.
        self._verify: bool | str = str(net.ca_bundle) if net.ca_bundle else True
        self._proxies: dict[str, str] = {}
        if net.https_proxy or net.http_proxy:
            self._proxies = {
                "https": net.https_proxy or net.http_proxy or "",
                "http": net.http_proxy or net.https_proxy or "",
            }

        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = user_agent(net)

    def _limiter_for(self, host: str) -> RateLimiter:
        with self._limiters_lock:
            if host not in self._limiters:
                limits = self._net.rate_limits
                rate = limits.get(host, limits.get("default_rps", 1.0))
                self._limiters[host] = RateLimiter(rate, sleep=self._sleep)
            return self._limiters[host]

    def _wait(self, state: RetryCallState) -> float:
        outcome = state.outcome
        if outcome is not None and not outcome.failed:
            delay = _retry_after_seconds(outcome.result().headers.get("Retry-After"))
            if delay is not None:
                return min(delay, MAX_RETRY_AFTER_S)
        # 2 s, 4 s, 8 s ... capped at 60 s, plus up to 1 s of jitter.
        return min(60.0, 2.0 * 2 ** (state.attempt_number - 1)) + random.uniform(0, 1)

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """GET ``url``. Never raises; inspect ``ok`` / ``error_kind`` on the result."""
        if params:
            url = requests.Request("GET", url, params=dict(params)).prepare().url or url
        shown_url = redact_url(url)
        limiter = self._limiter_for(urlsplit(url).hostname or "")
        attempts = 0

        def send() -> requests.Response:
            nonlocal attempts
            attempts += 1
            limiter.acquire()
            return self._session.get(
                url,
                headers=dict(headers or {}),
                timeout=self._timeout,
                proxies=self._proxies or None,
                verify=self._verify,
            )

        retrying = Retrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=self._wait,
            retry=retry_if_exception(_is_transient)
            | retry_if_result(lambda r: r.status_code in RETRY_STATUSES),
            # Out of attempts: hand back the last response, or re-raise the last exception.
            retry_error_callback=lambda state: state.outcome.result(),
            sleep=self._sleep,
            before_sleep=lambda s: log.info(
                "retrying %s (attempt %d failed)", shown_url, s.attempt_number
            ),
        )
        started = time.monotonic()
        try:
            resp = retrying(send)
        except Exception as exc:  # noqa: BLE001 - the contract is "never raises"
            kind = classify_error(exc)
            # INFO, not WARNING: callers record failures in their own logs and tables.
            log.info("GET %s failed (%s): %s", shown_url, kind, exc)
            return FetchResult(
                url=shown_url,
                status=None,
                content=b"",
                headers={},
                final_url=shown_url,
                elapsed_s=time.monotonic() - started,
                attempts=attempts,
                error_kind=kind,
                error=short_error(exc),
            )
        result = FetchResult(
            url=shown_url,
            status=resp.status_code,
            content=resp.content,
            headers=dict(resp.headers),
            final_url=redact_url(resp.url),
            elapsed_s=time.monotonic() - started,
            attempts=attempts,
        )
        if not result.ok:
            result.error_kind = "http"
            result.error = f"HTTP {resp.status_code} {resp.reason}"
        log.debug("GET %s -> %s (%d bytes, %.1fs)", shown_url, resp.status_code,
                  len(resp.content), result.elapsed_s)
        return result
