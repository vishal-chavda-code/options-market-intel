# omi: options market intelligence

Archives free public options data every day and serves it to a Streamlit dashboard focused
on **ETF vs single-stock vs index options** (index split broad / narrow, VIX inside index),
down to **underlying × exchange × expiry × customer / firm / market maker**. Design and verified
source facts are in [PROJECT_SPEC.md](PROJECT_SPEC.md); start with its Section 0.

> **Start the daily job at work as soon as possible.** OCC keeps only ~30 sessions of the
> per-symbol contract-volume file. Anything older than that is gone, so the archive only
> grows while `omi daily` runs every evening.

## What's in it

| Source | What it adds | History |
|---|---|---|
| `occ_contract_volume` | Every underlying × option root × exchange × expiry, split customer / firm / MM (all exchanges) | ~30 sessions at OCC, then whatever we archive |
| `occ_symbol_volume` | Calls vs puts × account × exchange for `universe` + top-N underlyings | ~2 years |
| `occ_weekly_class` | ETF / single stock / index: opening vs closing, premium, customer trade size | ~2 years |
| `occ_daily_volume` | Market-wide daily totals (equity incl. ETF vs index) | ~2 years |
| `cboe_daily` | Cboe exchange only: open interest and put/call ratios by category | years (via `?dt=`) |
| `symbol_directory` | ETF Y/N flag for every US listing (Nasdaq Trader) | current, snapshotted daily |

ETF vs single stock comes from the symbol directory, and index vs stock from OCC's product code.
Index broad vs narrow comes from [config/reference/index_underlyings.csv](config/reference/index_underlyings.csv),
a hand-maintained list (no free feed publishes it); rows marked `low` need confirming.

Volumes in OCC files count **both sides** of each trade; the dashboard shows
contracts = sides / 2. Verified: this reproduces OCC's published daily totals exactly.

## Setup (Windows)

Needs Python 3.11+ and access to PyPI (directly or through the proxy).

```powershell
git clone <repo-url> options-market-intel
cd options-market-intel
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1        # add -Dev for pytest + ruff
```

This creates `.venv\`, installs `omi`, and copies `config\config.example.yaml` to
`config\config.yaml` (git-ignored). Set `network.user_agent_contact` to a work email there.

## Run it

```powershell
scripts\run_probe.ps1                    # 1. can this network reach every source?
.venv\Scripts\omi import-seed            # 2. load the committed snapshot (seed\, see seed\README.md)
.venv\Scripts\omi backfill               # 3. fill the rest of history for every enabled source
.venv\Scripts\omi daily                  # 4. nightly: last session + 5-session catch-up, then features
.venv\Scripts\omi health                 # freshness and errors per source
scripts\run_app.ps1                      # dashboard at http://localhost:8501
```

Re-running any of these is safe. Anything already stored as `ok` is skipped. Dates a source
hasn't published yet are logged `not_available` and retried next time.

Backfill one source or range: `omi backfill -s occ_weekly_class --start 2025-01-01`.
Expected first-backfill times at the default polite rate (1 request/s per host, 0.5/s for
www.cboe.com):

| Source | Requests | Time |
|---|---|---|
| `occ_contract_volume` | ~30 (9 MB each) | ~2 min |
| `occ_weekly_class` | ~315 | ~6 min |
| `occ_daily_volume` | ~25 | < 1 min |
| `cboe_daily` from 2019-10-07 | ~1,780 | ~1.5 h |
| `occ_symbol_volume` from 2024-11-01, 55 symbols | ~27,000 | ~8 h (run overnight, or pass a later `--start`) |

Scheduling (`register_tasks.ps1`, 19:30 ET daily + 07:30 ET retry) is the next phase. Until then,
create a Windows Task Scheduler task that runs `.venv\Scripts\omi.exe daily` from the repo folder.

## Dashboard pages

- **Home**: daily contracts by class, ETF / single-stock / index share, customer share,
  index options by family (S&P 500, VIX, Russell, Nasdaq-100…), expiry mix (0DTE … 91d+), and
  the two-year OCC daily totals.
- **Symbols**: every underlying ranked for a day. Select one to see account mix, expiry
  buckets, exchange share, calls vs puts (when it's in the call/put set), and every expiry.
- **Weekly by class**: two years of ETF vs single stock vs index: contracts, premium,
  customer opening share, opening buys vs sells, small-lot (1–10) share as a retail proxy.
- **Cboe open interest**: open interest and P/C ratios by category (Cboe exchange only).
- **Data health**: per-source freshness, archive coverage, recent problems.

The app is read-only: it never calls external sources. Every chart has a table view with a CSV
download. Badges are green (current), orange (1 session behind) or red (stale or failing).

## Corporate network troubleshooting

All HTTP goes through `src/omi/http.py`. Settings live under `network:` in `config/config.yaml`,
and any value can be overridden by env var (e.g. `OMI_NETWORK__HTTPS_PROXY`).

| Symptom in probe | Likely cause | Fix |
|---|---|---|
| `[ssl] … CERTIFICATE_VERIFY_FAILED` | SSL inspection re-signs HTTPS with a corporate root CA | `use_os_truststore: true` (Windows cert store), **or** `ca_bundle: C:/path/corp-root.pem` |
| `[dns]` on every host | network only allows traffic via a proxy | `https_proxy: http://proxy.host:port` (ask IT for host:port if they give you a PAC URL) |
| `[proxy]` | wrong proxy, or it needs credentials | check host:port; `http://user:password@host:port` (redacted in logs) |
| `[timeout]` / `[connection]` on some hosts | host blocked by the firewall | ask IT to allow it (list below) |
| `WARN … got an HTML page` | proxy block page served with HTTP 200 | open the URL in a browser on that machine |
| `[http] HTTP 403` / `407` | proxy denies the domain / wants auth | as above |

Hosts to allow: `marketdata.theocc.com`, `www.nasdaqtrader.com`, `www.cboe.com`,
`cdn.cboe.com` **and** `cdn-api.cboe.com` (redirect), `www-api.cboe.com`; plus
`publicreporting.cftc.gov`, `home.treasury.gov`, `fred.stlouisfed.org` for sources probed but
not yet ingested. See PROJECT_SPEC.md §5.0.

## What to do when something turns red

| Status in `omi health` / Data health | Meaning | Action |
|---|---|---|
| `not_available` for today | source hasn't published yet | nothing; the next run retries |
| `not_available` for old dates | outside the source's history window | nothing (contract volume: ~30 sessions) |
| `network_error` / `http_error` | network or the source is down | run `omi probe`; see troubleshooting above |
| `schema_drift` | the source changed its layout | raw file is kept in `data/raw/…`; fix the parser against it, then re-run `omi backfill --force` for those dates |
| `parse_error` | unexpected content | same as schema drift |

## Development

```powershell
.venv\Scripts\python -m pytest            # offline tests on real (trimmed) public fixtures
.venv\Scripts\python -m pytest -m live    # hits the real endpoints once each
.venv\Scripts\ruff check src tests app
```

```
src/omi/config.py        settings: YAML + OMI_* env overrides
src/omi/http.py          the only network code: proxy, CA/truststore, UA, rate limit, retries
src/omi/calendar.py      NYSE sessions; OCC week endings (calendar Fridays)
src/omi/storage.py       raw gz archive, Parquet tables with PK upserts, ingest log
src/omi/runner.py        plan → skip stored → fetch → raw → parse → conform → commit
src/omi/sources/         one module per source (pure planners + parsers)
src/omi/features/        product-class classification and daily aggregates (DuckDB)
src/omi/pipeline.py      backfill / daily / health jobs
app/                     Streamlit dashboard (read-only)
config/reference/        exchange codes, index broad/narrow list
```

## Known fragile

- **OCC contract-volume history is ~30 sessions.** Missed days can't be recovered later.
- **Narrow vs broad index** is our own list; several Cboe products are `unclassified`.
- **ETF flag** uses today's listings for history (ETF status rarely changes).
- **Cboe pages** moved once already (`/us/options/market_statistics/daily/` →
  `/markets/us/options/market-statistics/daily`). The page doesn't print its own date, and
  for today it has no tables until Cboe publishes.
- **`occ_symbol_volume`** is one request per symbol per day; widen `top_n` with care.
- **Cboe delayed-quote JSON** (chains, not ingested yet) is undocumented; compliance sign-off needed.
