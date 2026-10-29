"""``omi probe``: one request per source, to see what the network lets through.

For each target it records the HTTP status, size, content type, redirects and a light
*format* check (is this CSV / JSON / an HTML page with tables, as expected?). It is not a
parser; it answers "can this machine reach the source and does the reply look right?".

Nothing is written except a small CSV log in ``logs/``. Response bodies are only saved with
``--save-samples``, which is meant for recording test fixtures on the personal machine.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from omi.calendar import last_week_ending_before, yyyymmdd
from omi.config import Settings
from omi.http import FetchResult

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
_RANK = {PASS: 0, WARN: 1, FAIL: 2, SKIP: 3}
_STYLE = {PASS: "green", WARN: "yellow", FAIL: "red", SKIP: "dim"}
_EXT = {"csv": ".csv", "json": ".json", "html": ".html", "text": ".txt"}


@dataclass(frozen=True)
class ProbeTarget:
    source: str  # key under `sources:` in the config
    name: str  # what is being fetched, in plain words
    url: str
    expect: str  # csv | json | html | text: what a healthy reply looks like
    require: str | None = None  # json: dotted path that must be a non-empty list/dict
    skip_reason: str | None = None  # set when the target cannot run (e.g. missing API key)
    secret: str | None = None  # value to scrub from anything displayed or logged


@dataclass
class ProbeRow:
    source: str
    target: str
    url: str
    result: str  # PASS | WARN | FAIL | SKIP
    http_status: int | None = None
    bytes: int = 0
    content_type: str = ""
    final_url: str = ""
    elapsed_s: float = 0.0
    attempts: int = 0
    error_kind: str = ""  # dns | ssl | proxy | timeout | connection | http | format | ...
    detail: str = ""


class Fetcher(Protocol):
    def get(self, url: str) -> FetchResult: ...


def build_targets(settings: Settings, as_of: date) -> list[ProbeTarget]:
    """One known-good URL per source (two for FRED: key-free CSV and the keyed API)."""
    d8, iso, year = yyyymmdd(as_of), as_of.isoformat(), as_of.year
    cot_dataset = (settings.source("cftc_cot").model_extra or {}).get("dataset", "gpe5-46if")
    key_env = (settings.source("fred").model_extra or {}).get("api_key_env", "FRED_API_KEY")
    fred_key = os.environ.get(key_env, "").strip()

    week = last_week_ending_before(as_of)
    occ = "https://marketdata.theocc.com/"
    return [
        ProbeTarget(
            "symbol_directory", "Nasdaq Trader listings (ETF flag)",
            "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
            "text",
        ),
        ProbeTarget(
            "occ_contract_volume", f"contract volume {iso} (~9 MB)",
            f"{occ}cont-volume-download?reportDate={d8}&format=txt",
            "text",
        ),
        ProbeTarget(
            "occ_symbol_volume", f"volume query SPY {iso}",
            f"{occ}volume-query?reportDate={d8}&format=csv&volumeQueryType=O&symbolType=U"
            "&symbol=SPY&reportType=D&accountType=ALL&productKind=ALL&porc=BOTH",
            "csv",
        ),
        ProbeTarget(
            "occ_weekly_class", f"weekly ETF report {week.isoformat()}",
            f"{occ}weekly-volume-reports?reportDate={yyyymmdd(week)}&reportType=options"
            "&reportClass=etf&format=csv",
            "csv",
        ),
        ProbeTarget(
            "occ_daily_volume", f"daily volume statistics {iso}",
            f"{occ}daily-volume-statistics?reportDate={d8}&format=csv",
            "csv",
        ),
        ProbeTarget(
            "cboe_indices", "VIX index history",
            "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
            "csv",
        ),
        ProbeTarget(
            "cboe_daily", f"options daily market statistics {iso}",
            f"https://www.cboe.com/markets/us/options/market-statistics/daily?dt={iso}",
            "html",
        ),
        ProbeTarget(
            "cboe_chains", "delayed option chain _VIX",
            "https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json",
            "json", require="data.options",
        ),
        ProbeTarget(
            "cfe_futures", "CFE settlement prices",
            "https://www-api.cboe.com/us/futures/market_statistics/settlement/csv",
            "csv",
        ),
        ProbeTarget(
            "cftc_cot", f"TFF {cot_dataset} (1 row)",
            f"https://publicreporting.cftc.gov/resource/{cot_dataset}.json?$limit=1",
            "json",
        ),
        ProbeTarget(
            "treasury", f"par yield curve {year}",
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
            f"&field_tdr_date_value={year}&page&_format=csv",
            "csv",
        ),
        ProbeTarget(
            "fred", "VIXCLS via fredgraph (no key)",
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
            "csv",
        ),
        ProbeTarget(
            "fred", "VIXCLS via API (key)",
            "https://api.stlouisfed.org/fred/series/observations"
            f"?series_id=VIXCLS&api_key={fred_key or 'MISSING'}&file_type=json",
            "json", require="observations",
            skip_reason=None if fred_key else f"{key_env} not set",
            secret=fred_key or None,
        ),
        ProbeTarget(
            "finra_regsho", f"Reg SHO short volume {iso}",
            f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d8}.txt",
            "text",
        ),
    ]


def _looks_like_html(head: str) -> bool:
    head = head.lstrip("﻿ \t\r\n").lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head[:512]


def _dig(obj: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def check_format(expect: str, body: bytes, require: str | None = None) -> tuple[bool, str]:
    """Does ``body`` look like the expected kind of payload? Returns (ok, short detail)."""
    if not body:
        return False, "empty body"
    text = body.decode("utf-8", errors="replace")
    is_html = _looks_like_html(text[:2048])

    if expect == "html":
        if not is_html:
            return False, "expected an HTML page"
        n_tables = len(re.findall(r"<table\b", text, flags=re.IGNORECASE))
        if n_tables == 0:
            return False, "HTML has no <table>: date not published yet, or the page changed"
        return True, f"{n_tables} <table> tags"

    if is_html:
        return False, f"got an HTML page, expected {expect}: block page or moved endpoint?"
    # OCC answers HTTP 200 with a one-line message when a report is not published.
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if first_line.startswith(("File requested does not exist", "Report is not available")):
        return False, f"source says: {first_line}"

    if expect == "json":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            return False, f"invalid JSON: {exc.msg} at char {exc.pos}"
        if require is not None:
            node = _dig(obj, require)
            if not node:
                return False, f"JSON lacks a non-empty '{require}'"
            return True, f"{require}: {len(node)} items"
        if isinstance(obj, list):
            return bool(obj), f"list of {len(obj)}"
        keys = list(obj)[:6] if isinstance(obj, dict) else []
        return True, f"keys: {', '.join(keys)}"

    if expect == "csv":
        rows = [r for r in csv.reader(io.StringIO(text)) if len(r) >= 2]
        if len(rows) < 2:
            return False, "fewer than 2 delimited rows"
        header = ",".join(c.strip() for c in rows[0] if c.strip())
        return True, f"{len(rows)} rows; header: {header[:60]}"

    lines = [ln for ln in text.splitlines() if ln.strip()]
    return True, f"{len(lines)} lines; first: {lines[0][:50]}"


def _scrub(text: str, secret: str | None) -> str:
    return text.replace(secret, "***") if secret else text


def probe_one(client: Fetcher, target: ProbeTarget) -> tuple[ProbeRow, bytes]:
    res = client.get(target.url)
    row = ProbeRow(
        source=target.source,
        target=target.name,
        url=_scrub(res.url, target.secret),
        result=FAIL,
        http_status=res.status,
        bytes=len(res.content),
        content_type=res.content_type.split(";")[0].strip(),
        final_url=_scrub(res.final_url, target.secret),
        elapsed_s=round(res.elapsed_s, 2),
        attempts=res.attempts,
    )
    if not res.ok:
        row.error_kind = res.error_kind or "other"
        row.detail = _scrub(res.error or "", target.secret)[:300]
        return row, res.content
    ok, detail = check_format(target.expect, res.content, target.require)
    row.result = PASS if ok else WARN
    row.error_kind = "" if ok else "format"
    row.detail = detail
    return row, res.content


def run_probe(
    settings: Settings,
    client: Fetcher,
    as_of: date,
    *,
    only: Iterable[str] = (),
    include_disabled: bool = False,
    save_dir: Path | None = None,
) -> list[ProbeRow]:
    only = set(only)
    rows: list[ProbeRow] = []
    for target in build_targets(settings, as_of):
        if only and target.source not in only:
            continue
        shown_url = _scrub(target.url, target.secret)
        if not include_disabled and not settings.source(target.source).enabled:
            rows.append(ProbeRow(target.source, target.name, shown_url, SKIP,
                                 detail="disabled in config"))
            continue
        if target.skip_reason:
            rows.append(ProbeRow(target.source, target.name, shown_url, SKIP,
                                 detail=target.skip_reason))
            continue
        row, body = probe_one(client, target)
        rows.append(row)
        if save_dir is not None and body:
            save_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^A-Za-z0-9_-]+", "_", target.name).strip("_")
            (save_dir / f"{target.source}__{slug}{_EXT[target.expect]}").write_bytes(body)
    return rows


def host_summary(rows: list[ProbeRow]) -> dict[str, str]:
    """Best result seen per host, counting both the requested and the post-redirect host."""
    best: dict[str, str] = {}
    for row in rows:
        if row.result == SKIP:
            continue
        hosts = {urlsplit(row.url).hostname}
        if row.final_url:
            hosts.add(urlsplit(row.final_url).hostname)
        for host in filter(None, hosts):
            if host not in best or _RANK[row.result] < _RANK[best[host]]:
                best[host] = row.result
    return dict(sorted(best.items()))


HINTS = {
    "ssl": "SSL errors usually mean the network inspects HTTPS traffic. Set network.ca_bundle "
           "to the corporate root CA (.pem), or network.use_os_truststore: true.",
    "proxy": "Proxy errors: check network.https_proxy (http://host:port, plus user:password@ "
             "if the proxy needs credentials).",
    "dns": "DNS lookups failed. If this network only allows traffic via a proxy, set "
           "network.https_proxy or the HTTPS_PROXY env var.",
    "timeout": "Timeouts often mean a firewall silently drops traffic to that host, or a "
               "proxy is required.",
    "connection": "Connection refused or reset: the host may be blocked, or a proxy is required.",
    "http": "HTTP 403/407 often comes from the corporate proxy blocking a domain; 404 means the "
            "endpoint moved (compare with PROJECT_SPEC.md).",
    "format": "A 200 reply in the wrong format is often a proxy block page. Open the URL in a "
              "browser on this machine to see what it returns.",
}


def render(rows: list[ProbeRow], as_of: date, console: Console) -> None:
    table = Table(title=f"omi probe (report date {as_of.isoformat()})", show_lines=False)
    for col, kw in [
        ("Source", {}), ("Target", {}), ("Result", {}), ("HTTP", {"justify": "right"}),
        ("Size", {"justify": "right"}), ("Type", {}), ("Secs", {"justify": "right"}),
        ("Redirect", {}), ("Detail", {"overflow": "fold"}),
    ]:
        table.add_column(col, **kw)
    for r in rows:
        redirect = ""
        if r.final_url and r.final_url != r.url:
            req, fin = urlsplit(r.url), urlsplit(r.final_url)
            redirect = f"-> {fin.hostname}" if fin.hostname != req.hostname else "-> new path"
        detail = r.detail if r.result != FAIL else f"[{r.error_kind}] {r.detail[:200]}"
        table.add_row(
            escape(r.source), escape(r.target), f"[{_STYLE[r.result]}]{r.result}[/]",
            "" if r.http_status is None else str(r.http_status),
            _human_bytes(r.bytes) if r.bytes else "", escape(r.content_type),
            f"{r.elapsed_s:.1f}" if r.result != SKIP else "", redirect, escape(detail),
        )
    console.print(table)

    hosts = host_summary(rows)
    if hosts:
        console.print("Hosts: " + "  ".join(f"[{_STYLE[v]}]{h} {v}[/]" for h, v in hosts.items()))
    kinds = sorted({r.error_kind for r in rows if r.error_kind})
    for kind in kinds:
        if kind in HINTS:
            console.print(f"[yellow]hint ({kind}):[/] {HINTS[kind]}")


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def write_log(rows: list[ProbeRow], logs_dir: Path) -> Path:
    """Write the probe results (no payloads) to logs/probe_<UTC timestamp>.csv."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = logs_dir / f"probe_{stamp}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["probed_at_utc", *asdict(rows[0]).keys()]
                                if rows else ["probed_at_utc"])
        writer.writeheader()
        for row in rows:
            writer.writerow({"probed_at_utc": stamp, **asdict(row)})
    return path


def exit_code(rows: list[ProbeRow]) -> int:
    """0 when every probed (non-skipped) target passed, else 1."""
    return 0 if all(r.result in (PASS, SKIP) for r in rows) else 1
