# Options Market Intelligence Pipeline + Streamlit Dashboard
### Build spec / master prompt for the AI coding assistant in VS Code

> **How to use this file**
> 1. Create an empty repo (e.g. `options-market-intel`) and drop this file in the root as `PROJECT_SPEC.md`.
> 2. Open Copilot Chat (agent mode) or Claude in VS Code and say:
>    *"Read PROJECT_SPEC.md end to end. Build Phase 0 only, then stop and show me what you built and how to run it."*
> 3. Go phase by phase (Section 12). Don't let the assistant build everything at once.
> 4. Keep this file in the repo. When a fact changes (an endpoint moves, a column changes), update it here first, then update the code.

---

## 0. Focus revision (2026-10-29), which takes precedence over later sections where they conflict

**What the user wants:** options activity split **ETF vs single stock vs index**, with index split **broad-based vs narrow-based**, at the **finest granularity available, per symbol where possible**. VIX options are part of index options (broad), broken out by family. The VIX index family and the VX futures curve (5.2, 5.5, most of 8.3) are **not needed**: `cboe_indices` and `cfe_futures` are disabled in config. COT, Treasury, FRED and chains stay as probed-only, not ingested yet.

### 0.1 Verified sources for the product-class split (all probed 2026-10-29)

| Source (config key) | URL | Granularity | History at OCC/Cboe | Notes |
|---|---|---|---|---|
| `occ_contract_volume` | `marketdata.theocc.com/cont-volume-download?reportDate={yyyymmdd}&format=txt` | **underlying × option root × exchange × expiry × customer / firm / market maker**, every optionable symbol, all exchanges | **~30 sessions only** (earliest available on 2026-10-29: 2026-09-17) | Fixed width, 52 chars per line; layout decoded and cross-checked (see `src/omi/sources/occ_contract_volume.py`). ~9 MB/day text, ~1 MB gzipped. No call/put, no strike. |
| `occ_symbol_volume` | `…/volume-query?reportDate={d}&format=csv&volumeQueryType=O&symbolType=U&symbol={SYM}&reportType=D&accountType=ALL&productKind=ALL&porc=BOTH` | underlying × root × exchange × account × **call/put** | ≥ 2 years (2024-11-01 worked) | One request per symbol per day: universe + top-N most active (config `top_n`). `contractDt` filters to one expiry date, so we omit it. `symbol` is required. |
| `occ_weekly_class` | `…/weekly-volume-reports?reportDate={Friday}&reportType=options&reportClass={etf\|equity\|index}&format=csv` | class × call/put × account (customer by trade size 1–10 / 11–49 / 50+, firm, MM) × **open/close buys/sells**, **contracts and premium**, + premium by exchange; standard and FLEX blocks | back to 2024-10-25 | `reportDate` must be the **calendar Friday, even when it is a market holiday** (Good Friday 2026-04-03 works, Thursday 04-02 doesn't). MMs are booked entirely as "close". `equity` excludes ETFs. |
| `occ_daily_volume` | `…/daily-volume-statistics?reportDate={d}&format=csv` | market-wide contracts/day: Equity (incl. ETF), Index/Others, Debt, Futures | ≥ 2 years | Month-to-date file (see 5.1 note). |
| `cboe_daily` | `www.cboe.com/markets/us/options/market-statistics/daily?dt={YYYY-MM-DD}` | Cboe exchange only: Index / ETP / Equity / VIX / SPX… call/put **volume and open interest**, P/C ratios | ≥ back to 2026-07-01 (earliest not yet probed) | Only free daily source here with OI by category. |
| `symbol_directory` | `www.nasdaqtrader.com/dynamic/SymDir/{nasdaqlisted,otherlisted}.txt` | every US listing with **ETF Y/N** | current only (snapshot daily) | [OBSERVED] Matching OCC underlyings (letters/digits only, e.g. `BRK.B` → `BRKB`) covered **100% of OSTK volume** on 2026-10-28 (4,300/4,301 underlyings; the miss was a bankrupt "Q" name). |

**OCC semantics verified on 2026-10-28:**
- **Sides, not contracts.** Customer + firm + MM volume counts both sides of each trade. Half the OSTK sides equals OCC's published Equity volume (50,854,822) **exactly**, and half the OIND sides equals Index/Others (5,025,345). The dashboard shows contracts = sides / 2.
- **Product kinds:** `OSTK` = stock **and ETF** options, `OIND` = index options (incl. VIX), `OCHF` = futures, `OOOF` = options on futures.
- **Unpublished dates** return HTTP 200 with `File requested does not exist.` (contract file) or `Report is not available.` (weekly). These are logged as `not_available` and retried, never marked ok.
- **Cross-source check:** VIX options from OCC (436,745 contracts) equal Cboe's VIX table total.
- **Exchange codes** (one letter) → names: `config/reference/occ_exchanges.csv`, from OCC's own XML.

**Narrow vs broad-based index:** no free feed labels it. It's maintained in `config/reference/index_underlyings.csv` with a `confidence` column. Rows marked `low` / `unclassified` need confirming. Narrow-based index options barely trade (XAU: 2 contracts on 2026-10-28). Most sector exposure trades through ETF options (XLF, SMH, …), which are classed as ETF.

### 0.2 Revised build phases (replace Section 12 phases 1–5 for now)

1. **Done 2026-10-29:** storage (raw gz archive, ingest log, PK upserts), runner, the six sources above, classification + features (`omi features`), CLI `backfill` / `daily` / `features` / `health`, dashboard pages *Home (product mix)*, *Symbols*, *Weekly by class*, *Cboe open interest*, *Data health*.
2. **Next:** run `omi daily` at work every evening (Task Scheduler). This is urgent because the contract-volume history is only ~30 sessions deep. Then: Phase 6 hardening (`register_tasks.ps1`, morning retry), per-strike detail for chosen symbols via Cboe chains (5.4), and the 0.3 open questions below.

### 0.3 Open questions from this revision
- Confirm the narrow/broad entries marked `low` / `unclassified` in `index_underlyings.csv`.
- ~~Should ETFs also be split broad-market vs sector/thematic?~~ **No** (user, 2026-10-29). ETFs stay one class.
- Which symbols beyond the top-N should always get the call/put query (`universe.extra`)?

---

## 1. What we're building and why

I run a **platform options exposure dashboard** at work. I want to add **market-wide options context** from the OCC, Cboe and other free public sources: who is trading, how positioning is shifting, the vol regime, positioning in the futures that matter, and upcoming events that can hit exposures (contract adjustments, special settlements, position-limit changes).

Deliverables:

1. **A data pipeline** that downloads free public data every day, keeps every raw file it downloads, turns it into clean tables, and builds a **growing historical archive**. Many free sources only keep a rolling window of history, so the archive itself is the valuable part. It starts accumulating the day the job first runs.
2. **A derived feature layer**: positioning z-scores, flow mix, vol term structure, chain analytics (gamma exposure, OI walls, skew) and regime flags.
3. **A Streamlit dashboard** that reads the stored tables, auto-refreshes, shows how fresh each dataset is, and can sit next to or feed into the existing exposure dashboard.

Non-goals: live trading signals, real-time tick data, or anything that needs a paid feed.

---

## 2. Hard constraints (read before writing code)

| # | Constraint | What it means for the code |
|---|---|---|
| C1 | **Free data only.** | No paid APIs, no API keys except free ones (FRED). No scraping paywalled sites. |
| C2 | **Code is written on a personal machine, pushed to GitHub, then cloned and run on the work machine.** The work machine has internet access. | The pipeline **runs at work** and downloads data there. Nothing assumes access to the personal machine at runtime. |
| C3 | **Data never travels back from work.** | `data/` is git-ignored. Never commit data, logs or outputs. Tests use fixtures recorded from *public* endpoints on the personal machine, never work data. *Exception (user request, 2026-10-29): a one-time `seed/` snapshot of public data downloaded on the personal machine is committed so the work machine starts with OCC history it can no longer download. It travels personal → work only; `data/` stays ignored; the repo must be private (§13).* |
| C4 | **Corporate network.** Possible proxy, SSL inspection, blocked domains. | All HTTP goes through one client that honours `HTTPS_PROXY`/`HTTP_PROXY` and an optional custom CA bundle path from config. Phase 0 is a connectivity probe. |
| C5 | **Likely Windows 10/11 + Python 3.11+.** | Use `pathlib` everywhere. No bash-only scripts; provide `.ps1`/`.bat` helpers plus a Windows Task Scheduler setup. Must also run on Linux/macOS for development. |
| C6 | **Validate assumptions before building on them.** This feeds risk context. | Never invent an endpoint or a column name. If a URL or schema in this spec doesn't match reality, **stop and write a probe**, report what's actually there, and update this spec. |
| C7 | **Sources change without notice.** | Each source is an isolated plugin. One broken source must never break the run. Failures are recorded and shown on the dashboard. |
| C8 | **Be polite to public servers.** | Rate-limit (default ≥1 s between requests per host), retry with backoff, identifying User-Agent, cache raw files, never re-download a date that was already stored successfully. |

---

## 3. Architecture

```
                 ┌──────────────────────── sources (plugins) ───────────────────────┐
                 │ OCC batch · Cboe indices CSV · Cboe daily stats · Cboe delayed    │
                 │ chains · CFE futures · CFTC COT · Treasury · FRED · (FINRA opt.)  │
                 └───────────────┬───────────────────────────────────────────────────┘
                                 │ fetch (http client: proxy, retry, rate limit, UA)
                                 ▼
  data/raw/{source}/{report}/{YYYY}/{as_of}__{hash8}.{ext}   ← kept forever; source of truth
                                 │ parse (pure functions; no network)
                                 ▼
  data/tables/{table}/year=YYYY/part-*.parquet               ← tidy, typed, deduped
                                 │ features (pure functions over tables)
                                 ▼
  data/features/{feature_table}.parquet                      ← dashboard-ready
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
         Streamlit app (reads via DuckDB)   exports/ (CSV/parquet for the
         auto-refresh fragments              existing exposure dashboard)

  data/meta/ingest_log.parquet   ← one row per fetch attempt (status, rows, sha256, timings)
```

**Query layer:** DuckDB reading Parquet directly (`read_parquet('data/tables/x/**/*.parquet')`). No server.

---

## 4. Repository layout

```
options-market-intel/
├─ PROJECT_SPEC.md
├─ README.md
├─ pyproject.toml              # deps + console script `omi`
├─ config/
│  ├─ config.example.yaml      # committed
│  └─ config.yaml              # git-ignored (local paths, proxy, FRED key)
├─ src/omi/
│  ├─ __init__.py
│  ├─ cli.py                   # typer: probe | backfill | daily | intraday | features | health
│  ├─ config.py                # pydantic settings loaded from yaml + env
│  ├─ http.py                  # the ONLY place requests are made
│  ├─ calendar.py              # NYSE trading days (exchange_calendars), report-date helpers
│  ├─ storage.py               # raw writer, parquet upsert, ingest_log, DuckDB helpers
│  ├─ quality.py               # schema/range/row-count/staleness checks
│  ├─ sources/
│  │  ├─ base.py               # Source protocol: plan(dates) -> requests; parse(raw) -> DataFrames
│  │  ├─ occ.py
│  │  ├─ cboe_indices.py
│  │  ├─ cboe_daily_stats.py
│  │  ├─ cboe_chains.py
│  │  ├─ cfe_futures.py
│  │  ├─ cftc_cot.py
│  │  ├─ treasury.py
│  │  ├─ fred.py
│  │  └─ finra_regsho.py       # disabled by default (see 5.8)
│  ├─ features/
│  │  ├─ positioning.py
│  │  ├─ flow.py
│  │  ├─ vol_regime.py
│  │  ├─ chain_analytics.py
│  │  ├─ cot.py
│  │  └─ events.py
│  └─ greeks.py                # Black-Scholes fallback (scipy) if a chain lacks greeks
├─ app/
│  ├─ Home.py                  # Streamlit entry
│  ├─ pages/                   # one file per page (Section 9)
│  └─ components.py            # freshness badges, metric tiles, chart helpers
├─ scripts/
│  ├─ run_daily.ps1
│  ├─ run_intraday.ps1
│  ├─ register_tasks.ps1       # creates Windows scheduled tasks
│  └─ run_app.ps1
├─ tests/
│  ├─ fixtures/                # recorded PUBLIC responses, small and trimmed
│  ├─ test_parsers_*.py
│  ├─ test_features_*.py
│  └─ test_live_smoke.py       # @pytest.mark.live — off by default
└─ .gitignore                  # data/, config/config.yaml, logs/, exports/, .venv/
```

**Dependencies:** `requests`, `tenacity`, `pandas`, `pyarrow`, `duckdb`, `numpy`, `scipy`, `pydantic`, `pydantic-settings`, `pyyaml`, `typer`, `exchange_calendars`, `lxml`, `beautifulsoup4`, `streamlit>=1.37`, `plotly`, `pytest`, `ruff`.

---

## 5. Data source catalog

Status tags: **[VERIFIED]** means the URL pattern was confirmed in official or primary documentation during research (Oct 2026). **[OBSERVED]** means it works and is widely used but is undocumented and could change. **[DISCOVER]** means the endpoint exists but the exact URL must be found in browser DevTools during Phase 0/1.

For every source, the assistant must first **fetch one sample, save it to `tests/fixtures/`, and write the parser against the real payload**, not against this spec's description.

### 5.0 Hosts contacted (probe 2026-10-29, personal machine: all reachable)

These are the hosts the corporate proxy/firewall must allow. Redirects matter: a request to the first host is answered by the second.

| Host | Used by | Notes |
|---|---|---|
| `marketdata.theocc.com` | OCC batch reports | |
| `cdn.cboe.com` → `cdn-api.cboe.com` | Cboe index CSVs, delayed quotes/chains | `cdn.cboe.com` redirects to `cdn-api.cboe.com`; **allow both** |
| `www.cboe.com` | Cboe daily market statistics page | redirects to a new path on the same host |
| `www-api.cboe.com` | CFE settlement CSV | |
| `publicreporting.cftc.gov` | CFTC COT (Socrata) | |
| `home.treasury.gov` | Treasury par yield curve | slow: ~20 s per yearly file |
| `www.nasdaqtrader.com` | Nasdaq Trader symbol directory (ETF flag) | added 2026-10-29 (Section 0) |
| `fred.stlouisfed.org` | FRED key-free CSV | |
| `api.stlouisfed.org` | FRED API | only with `FRED_API_KEY`; not yet probed |
| `cdn.finra.org` | FINRA Reg SHO | disabled; not probed |
| `www.theocc.com` | OCC contract-adjustment memos | Phase 1 [DISCOVER]; not probed |

### 5.1 OCC (Options Clearing Corporation) [VERIFIED]

Base: `https://marketdata.theocc.com/`. OCC publishes batch-processing URL examples for most reports. Dates are `yyyymmdd`. History is a rolling window: daily query screens hold about 2 years, historical volume stats about 5 years. **This is why we archive daily.**

| Report | URL template | Use |
|---|---|---|
| Volume Statistics, Daily | `daily-volume-statistics?reportDate={d}&format=csv` | ~~Market-wide call/put volume (and OI where present) by product class.~~ **Probe 2026-10-29:** month-to-date daily contract volume by product class (Equity, Index/Others, Debt, Futures). **No call/put split, no OI.** See note below the table. Core daily table. |
| Volume by Account Type, Download | `onn-volume-download?productKind=options&reportType=D&reportDate={d}&reportFormat=volume&issues=all&reportView=raw` (`reportFormat`: `volume`/`percent`/`both`; `reportType`: D/W/M/R/Y; Y uses `reportYear=yyyy`) | **Customer vs Firm vs Market-Maker** volume, all issues. The most distinctive free dataset here. |
| Volume by Account Type, Search | `onn-volume-search?productKind=options&underlying={sym}&options={sym}&reportType=D&reportDate={d}&reportFormat=both&reportView=raw` | Same split for a single underlying (universe names). |
| Volume Query | `volume-query?reportDate={d}&format=csv&volumeQueryType=O&symbolType=U&symbol={sym}&reportType=D&accountType=ALL&productKind=ALL&porc=BOTH&contractDt={d}` | Per-symbol volume by account type / product / put-call. **Check how `contractDt` behaves**; it may filter by expiry. |
| Exchange Volume | `exchange-volume?reportView=regular&reportType=D&reportDate={d}&instrumentType=options&format=txt` (`reportView=market` for share) | Exchange market share. |
| Volume Statistics, Weekly | `weekly-volume-reports?reportDate={d}&reportType=options&reportClass={equity|etf|index|debt}&format=csv` | Weekly rollups by class. |
| Volume Statistics, Monthly | `monthly-volume-reports?reportDate={d}&reportType=options&reportClass={equity|etf|index|debt}&format=csv` | Monthly rollups; long-run history. |
| Contract Volume Download | `cont-volume-download?reportDate={d}&format=txt` | Volume by contract. Large, so **probe size first**. |
| Trade Volume Download | `trade-volume-download?reportDate={d}&format=xml` | Alternative per-series volume. Probe; pick one of this or the above. |
| Futures Open Interest | `futures-open-interest?reportDate={d}&format=csv` | OCC-cleared futures OI. |
| Exchange Volume by Class | `volbyclass-reports?reportDate={d}&reportClass={exchange}&format=csv` | Optional. |
| Position Limits | `position-limits?reportDate={d}` and `position-limits?reportType=change` | **Events**: limit changes on held names. |
| Equity Special Settlements | `ess-reports?reportDate={d}` | **Events**: special settlements. Exposure-relevant. |
| Threshold Securities | `threshold-securities?reportDate={d}` | Reg SHO threshold list; flag held names. |
| Series Search | `series-search?symbolType=U&symbol={sym}` | Listed series and strikes per underlying (reference). |
| Directory of Listed Products | `delo-download?prodType=EU&downloadFields=OS;US&format=txt` | Symbol ↔ underlying mapping. Weekly refresh is enough. |
| Flex Reports | `flex-reports?reportType=PR&optionType=E&reportDate={d}` | Optional: FLEX activity. |

Also on OCC's site, with no batch URL documented: **contract adjustment info memos** (deliverable changes after splits/mergers/special dividends). Treat as **[DISCOVER]**. Find the memo listing's backing request in DevTools; if there isn't a clean one, scrape the listing page's title/date/link and flag memos that mention universe symbols.

Parsing notes: OCC CSV/TXT files often have preamble lines, footer totals and inconsistent headers across report types. Write one parser per report, keep the raw file, and add unit tests built from real fixtures. Store `report_date`, `fetched_at`, `source_url`.

**Probe 2026-10-29 (personal machine):**
- `daily-volume-statistics` (fixture `tests/fixtures/occ/daily_volume_statistics_20261028.csv`, 2.7 KB, served as `application/octet-stream`, CRLF). Two blocks:
  1. Title `Daily OCC Contract Volume - October 2026`, header `Date,Equity,Index/Others,Debt,Futures,OCC Total`, one row per session from the 1st of the month **through** `reportDate` (dates `MM/DD/YYYY`), then `Oct Total`, `Oct Avg`, `YTD Total.`, `YTD Avg.` rows.
  2. Title `Daily Futures Contract Volume -October 2026`, header `Date,Equity,Index/Others,OOF,OCC Total`, same row pattern.
  Numbers are quoted with thousands separators (`"50,854,822"`); every data row ends with a trailing comma. `OCC Total` = Equity + Index/Others + Debt + Futures. Because each file holds the whole month to date, **one fetch per month-end date backfills a month**. Market-wide call/put split (needed for 8.1 OCC P/C) must come from another OCC report (Volume Query or the account-type reports). Probe them in Phase 1.
- `onn-volume-download … reportView=raw` returns **fixed-width text, not CSV**: 1.7 MB, ~40.5k lines for one day. First line `H10282026      10282026`, then lines like `ZYME  ZYME  M000000217000000000000000217`: two 6-char symbol columns (option symbol, underlying?), a one-letter code, then three 9-digit numbers. What the letter and the three numbers mean is **not** self-describing. Phase 1 must establish the layout (try other `reportView` values, compare against the website's rendered table) before writing a parser.

### 5.2 Cboe volatility & strategy index history [VERIFIED pattern]

`https://cdn.cboe.com/api/global/us_indices/daily_prices/{SYMBOL}_History.csv`. The full history comes back in one file (VIX back to 1990, VVIX to 2007). Re-download daily, overwrite the table, and keep the raw file.

Symbols to try (pattern confirmed for VIX and VVIX; **probe each and record which exist**): `VIX, VIX9D, VIX1D, VIX3M, VIX6M, VIX1Y, VVIX, SKEW, VXN, RVX, VXD, GVZ, OVX, EVZ, VXEEM, VXAZN/VXAPL/VXGOG (single-stock VIXes, if present), PUT, BXM, BXMD, CNDR (strategy benchmarks)`.
Columns vary: VIX has OHLC; VVIX and some others are close-only. Normalise to long format `date, symbol, open, high, low, close`, with NaN where a column is missing.
Fallback for SKEW if the pattern 404s: find the link on the SKEW index page.

Older archive files: `https://cdn.cboe.com/resources/us/indices/vixarchive.xls` (VIX 1990–2003, old methodology). Optional.

**Probe 2026-10-29:** `VIX_History.csv` works: `cdn.cboe.com` redirects to `cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv`. 463 KB, header `DATE,OPEN,HIGH,LOW,CLOSE`, dates `MM/DD/YYYY`, first row `01/02/1990`. Fixture: `tests/fixtures/cboe_indices/VIX_History_trimmed.csv`. The other symbols are still to probe in Phase 2.

### 5.3 Cboe U.S. Options daily market statistics [VERIFIED page, DISCOVER backend]

Page: `https://www.cboe.com/us/options/market_statistics/daily/?dt=YYYY-MM-DD`
Contains Cboe-exchange **put/call ratios** (Total, Index, ETP, Equity, VIX, SPX+SPXW, XSP, others) plus **call/put volume and open interest** by category (Sum of All Products, Index, ETP, Equity, VIX, SPX…).
- Phase 1 task: open the page with DevTools → Network and find the JSON/CSV request that populates the tables. Use that if it exists; otherwise parse the HTML tables (`pandas.read_html` / bs4).
- The `?dt=` parameter allows **backfill**. Probe how far back it works and record the earliest date in the spec.
- Legacy CSVs on the historical-data page cover 2006 to 2019-10-04 only. Ingest once for long history: `https://www.cboe.com/us/options/market_statistics/historical_data/`.
- Older text archive (2000s): `https://cdn.cboe.com/resources/us/options/market_statistics/daily/cone/archive/html/{YYYY-MM-DD}.html`. Optional; low priority.
- **Probe 2026-10-29:** the page URL above now redirects to `https://www.cboe.com/markets/us/options/market-statistics/daily?dt=YYYY-MM-DD`. The tables are **server-rendered** (Next.js; ~450 KB HTML), so `pandas.read_html` works without JavaScript. `dt=2026-10-28` had 27 `<table>` elements. The first is `Ratios | Value` (TOTAL PUT/CALL RATIO, INDEX…, EXCHANGE TRADED PRODUCTS…, EQUITY…, CBOE VOLATILITY INDEX (VIX)…, SPX + SPXW…, XSP…). Then one table per category (`SUM OF ALL PRODUCTS`, `INDEX OPTIONS`, `EXCHANGE TRADED PRODUCTS`, `EQUITY OPTIONS`, `CBOE VOLATILITY INDEX (VIX)`, …) with rows `VOLUME` / `OPEN INTEREST` and columns `Call` / `Put` / `Total`. A JSON backend is no longer required, but it's still worth a quick DevTools look in Phase 1. **Publication lag:** `dt=2026-10-29` fetched at 16:40 ET that day returned the page with **no tables**. Confirm the data is there by the 19:30 ET daily run. Earliest working `dt` is still unknown.

Related Cboe pages:
- **U.S. Options Market Volume Summary** (all U.S. exchanges, market share, plus intraday cumulative call/put by half hour): `https://www.cboe.com/us/options/market_statistics/market/`. It has a "Download this day as CSV" link. **[DISCOVER]** its URL and use it for (a) all-exchange daily volume and (b) an **intraday P/C ratio curve** snapshot.
- Most-active / symbol data pages: optional.

### 5.4 Cboe delayed quotes & full option chains [OBSERVED: undocumented, best-effort]

Base: `https://cdn.cboe.com/api/global/delayed_quotes/`
- Quote: `quotes/{SYM}.json`
- **Full chain**: `options/{SYM}.json`
- Indices use an underscore prefix: `_SPX`, `_VIX`, `_NDX`, `_RUT`. Equities and ETFs are plain (`SPY`, `QQQ`, `IWM`, `AAPL`).

Payload: the underlying price plus every listed contract with bid/ask/last, volume, **open interest, implied vol and greeks** (delta, gamma, theta, vega, rho). Option identifiers are OSI-style (`SPY260501C00400000` = root + YYMMDD + C/P + strike×1000). **Inspect the real JSON and map the actual keys.**

**Probe 2026-10-29 (`options/_VIX.json`, fixture `tests/fixtures/cboe_chains/_VIX_trimmed.json`):** `cdn.cboe.com` redirects to `cdn-api.cboe.com`. 658 KB, 1,520 contracts. Top level: `timestamp` (`"2026-10-29 20:27:39"` when fetched at 16:27 ET, so it **looks like UTC**; confirm), `symbol`, `data`.
- `data` (underlying): `symbol` (`^VIX`), `security_type`, `exchange_id`, `current_price`, `price_change`, `price_change_percent`, `bid`, `ask`, `bid_size`, `ask_size`, `open`, `high`, `low`, `close`, `prev_day_close`, `volume`, `iv30`, `iv30_change`, `iv30_change_percent`, `seqno`, `last_trade_time`, `tick`.
- `data.options[]`: `option` (e.g. `VIX261118C00010000`), `bid`, `bid_size`, `ask`, `ask_size`, `iv`, `open_interest`, `volume`, `delta`, `gamma`, `vega`, `theta`, `rho`, `theo`, `change`, `open`, `high`, `low`, `tick`, `last_trade_price`, `last_trade_time`, `percent_change`, `prev_day_close`.
- `iv` can be `0.0` (seen on a deep ITM call that still carried greeks). Treat 0 as missing, not as zero vol.

This is the engine for chain analytics (Section 8.4). Rules:
- Treat it as **fragile**. It's not a supported product. Wrap it in a schema check and fail soft.
- Data is delayed about 15 min. Label it as delayed everywhere in the UI.
- **EOD snapshot** after the close (~16:30 ET) for the full universe gives a daily chain history we build ourselves, which is otherwise expensive to buy.
- **Optional intraday** snapshots every 15–30 min during market hours, only for a small core set (`_SPX, SPY, QQQ, IWM, _VIX`), with a strict per-host rate limit.
- Storage: chains are big. Store EOD chains as Parquet partitioned by `underlying/year`, keep only needed columns, and compress with zstd. Intraday: keep 30 days of full snapshots plus derived aggregates forever.
- Check with compliance that internal use of Cboe website data is OK (Section 13).

### 5.5 Cboe Futures Exchange (CFE): VIX futures [VERIFIED pages, DISCOVER file URLs]

- Daily settlement prices for all CFE products (VX, VXM, VA, etc.): `https://www.cboe.com/markets/us/futures/market-statistics/settlement/futures/daily`. Gives the **VX term structure** each day.
- Daily volume/OI by product: `https://www.cboe.com/markets/us/futures/market-statistics/daily/futures/`.
- Historical per-contract CSVs (Trade Date, Futures, OHLC, Settle, Change, Total Volume, EFP, Open Interest), back to 2004: `https://www.cboe.com/us/futures/market_statistics/historical_data/`. **[DISCOVER]** the per-expiry CSV URL pattern from the links on that page, then backfill every VX expiry once.
- VIX settlement (SOQ) series files: `https://www.cboe.com/us/futures/market_statistics/vix_settlement_series` (`soq_vxs_YYYYMMDD.csv`). Optional.
- Also obtainable through the delayed-quotes API (Section 5.4) for VX futures. Use whichever is cleaner.
- **Probe 2026-10-29 [OBSERVED]:** the settlement page (first URL above) is server-rendered HTML with 38 tables. It links a CSV, `https://www-api.cboe.com/us/futures/market_statistics/settlement/csv` (redirects to the same path with a trailing `/`). Header `Product,Symbol,Expiration Date,Price`, 58 rows on 2026-10-29 covering VX (14 rows: weeklies like `VX44/X6` and monthlies like `VX/X6`), VXM, VA, XBTF, FBT, FET, IBHY, IBIG, IEMD, MGTN, PBT, PET. This is the cleanest daily VX term-structure source. Whether it accepts a date parameter for backfill is a Phase 2 question. The page also links `https://cdn.cboe.com/resources/futures/archive/volume-and-price/CFE_FinalSettlement_Archive.csv` (not yet fetched). Fixture: `tests/fixtures/cfe_futures/settlement.csv`.

### 5.6 CFTC Commitments of Traders [VERIFIED]

Socrata (SODA) API, **no key required**: `https://publicreporting.cftc.gov/resource/{dataset_id}.json` (or `.csv`), with SoQL params `$where`, `$select`, `$order`, `$limit`, `$offset`.

| Dataset | ID |
|---|---|
| Traders in Financial Futures (TFF), futures only | `gpe5-46if` |
| TFF, futures + options combined | `yw9f-hn96` |
| Legacy, futures only | `6dca-aqww` |
| Legacy, combined | `jun7-fc8e` |
| Disaggregated futures only / combined | `72hh-3qpy` / `kh3c-gbw2` |

Use **TFF** for: VIX futures (CFE), E-mini S&P 500, Nasdaq-100, Russell, 2Y/10Y Treasuries. Discover the contract codes by querying distinct `market_and_exchange_names, cftc_contract_market_code`, then pin them in config.
Positions are as of Tuesday, published Friday 3:30 pm ET. History runs from 2006 (TFF). Key fields: `report_date_as_yyyy_mm_dd`, `open_interest_all`, `dealer_positions_long_all/short_all`, `asset_mgr_positions_long/short`, `lev_money_positions_long/short`, `change_in_*`. Use the real column names from the payload.

**Probe 2026-10-29:** `gpe5-46if.json?$limit=1` works with no key and returns 89 fields per row. **Without `$order` the row is arbitrary**: it came back as UST BOND for 2022-09-13. Always pass `$order` (and `$where`) explicitly. `report_date_as_yyyy_mm_dd` is a timestamp string (`"2022-09-13T00:00:00.000"`), and `cftc_market_code` has trailing spaces (`"CBT "`). Fixture: `tests/fixtures/cftc_cot/gpe5-46if_limit1.json`.

### 5.7 Rates for greeks and context [VERIFIED]

- **U.S. Treasury par yield curve**, CSV by year:
  `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{YYYY}/all?type=daily_treasury_yield_curve&field_tdr_date_value={YYYY}&page&_format=csv`
  Send a descriptive User-Agent; the CDN blocks some defaults. Published around 15:30 ET. Use it for the risk-free rate in the BS fallback and for context tiles.
- **FRED**: `https://api.stlouisfed.org/fred/series/observations?series_id={ID}&api_key={KEY}&file_type=json` (free key; ~120 req/min). Key-free fallback: `https://fred.stlouisfed.org/graph/fredgraph.csv?id={ID}`.
  Series: `SP500` (for realized vol / VRP), `VIXCLS` (cross-check), `DGS3MO`, `DGS2`, `DGS10`, `T10Y2Y`, `DFF`, `BAMLH0A0HYM2` (HY OAS), `NFCI` (weekly).
- **Probe 2026-10-29:**
  - Treasury works with the omi User-Agent but is **slow**: ~20 s for the 16.6 KB 2026 file, so keep the 60 s read timeout. The header now includes a **`1.5 Month`** tenor: `Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"`. Rows are newest first, dates `MM/DD/YYYY`. The 10/29 row was already present at 16:28 ET.
  - FRED `fredgraph.csv` header is `observation_date,VIXCLS` (not `DATE`), with ISO dates. The FRED API was not probed (no key on this machine).
  - Fixtures: `tests/fixtures/treasury/…`, `tests/fixtures/fred/…`.

### 5.8 FINRA Reg SHO daily short-sale volume [OPTIONAL, disabled by default]

Files: `https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt`, pipe-delimited (`Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market`), header row plus a trailer row, posted by 6 pm ET.
**Leave `enabled: false` until compliance OKs it.** FINRA's website terms may restrict building databases from this data. FINRA also offers a separate API with its own terms. If enabled, filter to universe symbols only.

### 5.9 Bloomberg adapter [OPTIONAL hook]

We already license a terminal, so it doesn't break the "free" rule. Add an **interface only** (`sources/bloomberg.py`, disabled) for per-underlying IV surfaces and skew, to fill in later. Don't make the core pipeline depend on it.

### 5.10 Explicitly NOT used
`yfinance` / Yahoo scraping (terms-of-service risk at a regulated firm), paid vendors, anything behind a login.

---

## 6. Storage design

**Raw layer** `data/raw/...`: write once, never modified. Filename includes `as_of` date plus the first 8 characters of the content sha256. If the same content arrives again, skip writing it.

*As built (2026-10-29):* raw files are gzipped (`….{ext}.gz`, sha256 of the uncompressed bytes). The ingest log is a folder of small parts, `data/meta/ingest_log/part-{run_id}-{seq}.parquet`, so a crash can't corrupt one big file. Skips are counted in the run summary rather than logged as rows. Tables partition by `year=`, `month=` or `day=` as declared per table (contract volume is per day). Status `empty` is named `not_available`.

**Ingest log** `data/meta/ingest_log.parquet`: one row per attempt:
`run_id, source, report, as_of_date, url, http_status, bytes, sha256, rows_parsed, status{ok|empty|http_error|parse_error|schema_drift|skipped_exists}, error_msg, started_at, finished_at`.
Before fetching, check the log: if `(source, report, as_of_date)` already has `status=ok`, skip it unless `--force`. *(Same "append new days only, never re-pull" principle as my Bloomberg OHLC archive.)*

**Tables layer** `data/tables/{table}/year=YYYY/*.parquet`: tidy, typed, with a declared **primary key** per table. Upserts dedupe on the PK (last write wins, by `fetched_at`). Each table has a pydantic or pandera-style schema in code. A schema mismatch → `schema_drift` status, the raw file is kept, and nothing is written to the table.

Core tables (names are suggestions; finalize after probing):
- `occ_daily_volume (report_date, product_class{equity,index_other,debt,futures}, volume)`, plus the futures block `(report_date, product_class{equity,index_other,oof}, futures_volume)`. *Revised after probe 2026-10-29: this report has no call/put split and no OI (see 5.1).*
- `occ_volume_by_acct (report_date, scope{all|symbol}, symbol, account_type{C,F,M}, product_kind, call_vol, put_vol, total_vol, pct)`
- `occ_exchange_volume (report_date, exchange, volume, share)`
- `occ_events (report_date, event_type{ess|position_limit_change|threshold|contract_adjustment}, symbol, detail, url)`
- `cboe_index_history (date, symbol, open, high, low, close)`
- `cboe_daily_stats (date, category, call_vol, put_vol, total_vol, call_oi, put_oi, total_oi)`
- `cboe_pc_ratios (date, ratio_name, value)`
- `cboe_intraday_pc (date, time, category, calls, puts, pc_ratio)`
- `chain_eod (as_of, underlying, expiry, strike, cp, bid, ask, last, volume, oi, iv, delta, gamma, theta, vega, underlying_px)`
- `chain_intraday` (same columns plus `snapshot_ts`)
- `vx_futures (trade_date, contract, expiry, settle, volume, oi)`
- `cot_tff (report_date, market_code, market_name, oi, dealer_long, dealer_short, assetmgr_long, assetmgr_short, levmoney_long, levmoney_short, ...)`
- `rates_treasury (date, tenor, yield)`, `fred_series (date, series_id, value)`

**Features layer**: Section 8 outputs, one Parquet file per feature family, fully rebuilt each run (cheap). Each row carries `as_of`.

**Exports**: `exports/` gets small CSV/Parquet snapshots shaped for the existing exposure dashboard (Section 9.4).

---

## 7. Pipeline mechanics

**HTTP client (`http.py`)**
- `requests.Session` with: proxies from env/config, `verify=` custom CA bundle if configured, timeout (10 s connect / 60 s read), UA `omi/0.1 (internal research; contact: <config email>)`.
- `tenacity` retry on 429/5xx/connection errors with exponential backoff and jitter, max 4 attempts. Honour `Retry-After`.
- Per-host token-bucket rate limit (configurable; default 1 req/s, 0.5 req/s for cboe.com).
- Returns `(status, content_bytes, headers, final_url)`. Never raises past the source boundary.

**Calendar (`calendar.py`)**: NYSE sessions from `exchange_calendars` (`XNYS`). Utilities: `trading_days(start, end)`, `last_completed_session(now_et)`, `is_market_open(now_et)`. All timestamps stored in UTC; displayed in America/New_York.

**Source plugin contract (`sources/base.py`)**
```python
class Source(Protocol):
    name: str
    def plan(self, dates: list[date], cfg) -> list[FetchRequest]: ...   # what to download
    def parse(self, req: FetchRequest, raw: bytes) -> dict[str, pd.DataFrame]: ...  # table_name -> rows
    def expected_schema(self) -> dict[str, Schema]: ...
```
The runner owns fetching, raw storage, logging, validation and upserts. Sources are pure planners and parsers, which makes them easy to unit-test offline.

**CLI (`omi`)**
- `omi probe`: hits one known-good URL per source, prints a pass/fail table (status, bytes, content-type, parse ok?) and writes nothing except a probe log. **First thing to run at work.**
- `omi backfill --source occ --start 2024-10-01 --end today`: walks trading days, skips dates already logged as ok, sleeps between requests.
- `omi daily`: runs every enabled source for `last_completed_session`, then `features`, then `health`.
- `omi intraday`: chain plus intraday P/C snapshots for the core set, only if the market is open.
- `omi features`: rebuilds all feature tables.
- `omi health`: writes `data/meta/health.parquet` (latest ok date per table, staleness in sessions, last error).

**Scheduling** (`scripts/register_tasks.ps1` uses `schtasks` or `Register-ScheduledTask`):
- `omi daily` at **19:30 ET** Mon–Fri, with a retry run at **07:30 ET** next morning for anything still missing (OCC/Cboe sometimes publish late).
- CFTC COT pull on **Fridays 16:00 ET**.
- `omi intraday` every **15 min, 09:45–16:15 ET**, if enabled.
- Treasury and FRED are included in daily.
- Document the cron equivalents for Linux.

**Data quality (`quality.py`)**, run after each parse:
- Schema and dtype match; PK unique.
- Row count within the expected band (e.g. OCC daily product rows > 0; chain contracts > N).
- Value sanity: volumes ≥ 0; P/C between 0.05 and 10; IV between 0 and 5; VIX between 5 and 150.
- Day-over-day jump flag (|z| > 6 vs. 60-day history) → **warn, don't drop**.
- Staleness: a table more than 1 session behind after the morning retry → `stale`.
Results go to `data/meta/dq_results.parquet` and show on the Data Health page.

**Logging**: stdlib `logging` to `logs/omi_{date}.log` (git-ignored) plus the console. One summary line per source per run.

---

## 8. Features / analytics catalog

All rolling stats use **trading-day windows**, computed only on past data (no look-ahead). Default windows: 20d, 60d, 252d. Every feature records its inputs' as_of dates.

### 8.1 Positioning & sentiment
- P/C ratios (Cboe total, equity, index, ETP, SPX+SPXW, VIX), plus OCC-derived market-wide P/C (all exchanges).
- For each: level, 10d MA, **252d z-score** and **252d percentile**.
- Equity P/C minus index P/C spread (retail vs. hedger tone).
- Volume P/C vs. **OI P/C** (flow vs. stock of positions).

### 8.2 Who's trading (OCC account type)
- Customer share of total volume (all, and calls vs. puts); firm share; MM share.
- Customer call-volume share z-score (speculative call activity gauge).
- Product mix: equity / ETF / index share of total options volume; ETF share trend.
- Per-universe-symbol customer share vs. its own history (flags unusual customer flow in held names).
- Exchange market-share shifts (low priority).

### 8.3 Vol regime
- VIX level and percentile; **VIX9D/VIX**, **VIX/VIX3M**, **VIX3M/VIX6M** (ratio > 1 = inverted/backwardation flag); VIX1D/VIX9D for event stress.
- **VX futures curve**: M1, M2…; M1–M2 spread; annualized roll yield `(M2/M1 − 1) × 365 / days_between`; contango/backwardation flag.
- **VVIX**, **VVIX/VIX** ratio; **SKEW** level and percentile.
- **Variance risk premium proxy**: `VIX − RV21(SPX)×100`, where `RV21 = std(log returns, 21d) × sqrt(252)`, SPX from FRED `SP500`.
- Cross-asset vol: GVZ, OVX, VXN vs. VIX.
- **Regime label** (transparent rules, not ML): e.g. *Calm* (VIX pct < 30 & curve in contango), *Elevated*, *Stressed* (VIX/VIX3M > 1 or VIX pct > 90). Thresholds live in config.

### 8.4 Chain analytics (from 5.4 snapshots), per underlying
Use the chain's own greeks; fall back to BS (`greeks.py`, Treasury rate interpolated to expiry, dividend yield 0 by default, configurable) if they're missing.
- **ATM IV term structure** (by expiry) and **expected move** ≈ ATM straddle mid for the next 1–3 expiries.
- **25-delta risk reversal** and **25-delta butterfly** per expiry (interpolate IV by delta).
- **OI walls**: strikes with max call OI / max put OI (next expiry and all expiries); OI-weighted put/call strike centroid.
- **Max pain** per expiry (strike minimizing total intrinsic payout to holders).
- **Net gamma exposure (GEX), a heuristic**: per contract `gamma × OI × 100 × S² × 0.01` (dollar gamma per 1% move), signed **+calls / −puts** under the common "dealers long calls / short puts" assumption. Report total GEX, GEX by strike, and the **gamma-flip level** (the spot where cumulative GEX crosses zero, found by re-pricing gamma across a grid of spots). **The UI must label the sign convention as an assumption**, because dealer positioning isn't observable from public data.
- Day-over-day **OI change by strike/expiry** (where positioning was added).
- 0DTE share: volume in same-day expiries / total (SPX, SPY, QQQ).

### 8.5 Futures positioning (CFTC TFF)
- Net positions (long − short) for leveraged funds, asset managers and dealers in VIX, ES, NQ, ZN/ZT.
- Net as % of OI; 3-year (156-week) z-score and percentile; week-over-week change.
- Leveraged-fund VIX net short extremes (crowded short-vol flag).

### 8.6 Events & reference
- Upcoming standard and quarterly expirations (calendar) plus OPEX week flag.
- New OCC ESS, position-limit changes, threshold-list adds/drops and contract-adjustment memos **intersected with the universe** (config list, and later the exposure book's underlyings via a CSV the user drops in locally; never committed).

### 8.7 Composite (optional, Phase 5)
- "Market options stress" score = mean of selected percentile features (configurable weights), with a breakdown chart showing contributions. Transparent and explainable.

---

## 9. Streamlit dashboard

### 9.1 Behaviour
- Reads **only** from `data/features`, `data/tables` and `data/meta` through DuckDB. It never calls external sources.
- Data loaders use `@st.cache_data(ttl=300)`.
- **Auto-refresh**: wrap live sections in `@st.fragment(run_every="5m")` (Streamlit ≥ 1.37). Only that fragment reruns, not the whole app. The intraday chain section can use `run_every="60s"` during market hours and `None` otherwise (compute `run_every` dynamically inside the page function). Sidebar toggle to pause auto-refresh.
- Every chart or tile shows an **as-of timestamp** and a **freshness badge** (green = current session, amber = 1 session stale, red = older or last run failed), taken from `health.parquet`.
- "Delayed data" label on anything sourced from Cboe delayed quotes.
- Plotly for charts; consistent colours (calls = one hue, puts = another; regime colours fixed).
- Launch: `streamlit run app/Home.py --server.port 8501`. `scripts/run_app.ps1` wraps it.

### 9.2 Pages
1. **Overview**: regime label; tiles for VIX (pct), VIX/VIX3M, VX M1–M2, SKEW, VVIX, equity P/C z, customer call share z, SPX GEX sign and flip level, leveraged-fund VIX net pct. Each tile has a sparkline and a click-through to its page. Top banner if any source is red.
2. **Positioning**: P/C ratios with z-score bands, percentile heatmap (rows = ratios, cols = last 60 sessions), volume vs. OI P/C.
3. **Flow (OCC)**: customer/firm/MM stacked share over time; product mix; table of universe symbols ranked by customer-share z-score.
4. **Vol regime**: VIX family term structure (today vs. 1w/1m ago), VX futures curve with history slider, VVIX/SKEW, VRP chart, cross-asset vol.
5. **Chain analytics**: underlying selector; GEX by strike bar chart with spot and flip lines; OI walls; IV smile per expiry; ATM term structure; expected-move table; OI change heatmap (strike × expiry); intraday fragment when the market is open.
6. **Futures positioning (COT)**: net positions by trader class and market, with z-score shading.
7. **Events**: upcoming expirations; recent ESS / position-limit / threshold / adjustment items, with held-name hits highlighted.
8. **Data health**: per-source table (last ok date, last attempt, status, rows, error) from `ingest_log`/`health`/`dq_results`; DQ warnings list; history coverage chart (first and last date per table, showing how much archive we've built).

### 9.3 Layout & UX
- `st.set_page_config(layout="wide")`; sidebar: date range, universe selector, refresh toggle, "as-of" override (view history as of a past date).
- Download buttons (CSV) under each table.
- No secrets in the app; it's read-only.

### 9.4 Integration with the existing exposure dashboard
- `omi features` also writes `exports/market_context_latest.csv` (one row of headline metrics) and `exports/market_context_history.parquet`. The existing dashboard can read these from a shared path set in config.
- Optional later: a tiny FastAPI endpoint serving the same JSON. Not in the initial scope.

---

## 10. Testing

- **Fixtures**: for each source and report, record 1–3 real public responses on the personal machine, trimmed to stay small, into `tests/fixtures/{source}/`. Public market data only.
- **Parser tests**: fixture → expected DataFrame shape, dtypes, PK uniqueness, a few hand-checked values.
- **Feature tests**: synthetic inputs with known answers (z-score, roll yield, max pain on a toy chain, GEX sign on a toy chain, BS greeks vs. a reference value).
- **Runner tests**: fake source + fake HTTP; verify skip-if-ok, schema-drift handling, that one failing source doesn't stop the others, and idempotent upserts.
- **Live smoke** (`pytest -m live`): hits each endpoint once. Not in default runs; used during `omi probe` development.
- `ruff` for linting; type hints throughout. Optional GitHub Actions running offline tests on push (no network calls in CI).

---

## 11. Config (`config/config.example.yaml`)

```yaml
paths:
  data_dir: ./data
  exports_dir: ./exports
  logs_dir: ./logs
network:
  https_proxy: null          # or http://proxy.corp:8080 ; env vars also honoured
  ca_bundle: null            # path to corporate root CA .pem if SSL inspection
  user_agent_contact: you@example.com
  rate_limits: {default_rps: 1.0, "www.cboe.com": 0.5, "cdn.cboe.com": 1.0}
universe:
  core: [_SPX, SPY, QQQ, IWM, _VIX]
  extra: []                  # add single names; or point to a local CSV:
  holdings_csv: null         # local-only file of underlyings from the exposure book (never committed)
sources:
  occ:            {enabled: true,  backfill_start: "2024-11-01"}
  cboe_indices:   {enabled: true,  symbols: [VIX, VIX9D, VIX1D, VIX3M, VIX6M, VIX1Y, VVIX, SKEW, VXN, GVZ, OVX]}
  cboe_daily:     {enabled: true,  backfill_start: "2019-10-07"}
  cboe_chains:    {enabled: true,  eod: true, intraday: false, intraday_minutes: 15}
  cfe_futures:    {enabled: true}
  cftc_cot:       {enabled: true,  dataset: gpe5-46if, markets: []}   # fill codes after discovery
  treasury:       {enabled: true}
  fred:           {enabled: true,  api_key_env: FRED_API_KEY, series: [SP500, VIXCLS, DGS3MO, DGS2, DGS10, T10Y2Y, BAMLH0A0HYM2, NFCI]}
  finra_regsho:   {enabled: false}
features:
  windows: {short: 20, medium: 60, long: 252}
  regime: {calm_vix_pct: 30, stressed_vix_pct: 90, inversion_ratio: 1.0}
  gex_sign_convention: dealer_long_calls_short_puts
schedule:
  daily_et: "19:30"
  retry_et: "07:30"
  cot_et: "Fri 16:00"
app:
  refresh_seconds: 300
  intraday_refresh_seconds: 60
```

---

## 12. Build phases (go one at a time; each ends with a demo and the acceptance check)

**Phase 0: skeleton + connectivity probe**
Repo layout, `pyproject`, config loading, `http.py`, `calendar.py`, `omi probe` covering one URL per source.
✅ Accept: `pip install -e .` works; `omi probe` prints a status table. On the work machine, it tells us which domains get through the corporate network. *(Only pass/fail results get discussed back on the personal side. No payloads or files come back.)*

**Phase 1: archive the perishable stuff first**
`storage.py` (raw writer, ingest_log, parquet upsert), `quality.py`, and sources **OCC** (daily volume stats, volume by account type, exchange volume, ESS, position limits, threshold) and **Cboe daily stats** (+ discover backend), with backfill as far as each source allows.
✅ Accept: `omi backfill` + `omi daily` populate tables; re-running is a no-op; the ingest log shows ok/skip; parser tests pass.

**Phase 2: vol & macro context**
Cboe index CSVs, CFE VX settlements + historical VX contracts, Treasury, FRED, CFTC TFF.
✅ Accept: VIX family, VX curve, rates and COT tables populated with full history; DQ checks green.

**Phase 3: chains**
Delayed-chain fetcher (EOD for universe, optional intraday for core), schema guard, `chain_eod` storage, BS fallback.
✅ Accept: an EOD snapshot for 5 core underlyings stored; contract counts are sensible; greeks present or back-filled.

**Phase 4: features**
Sections 8.1–8.6 implemented with tests; `exports/` written.
✅ Accept: feature tables rebuild in under 2 min; toy-chain tests pass (max pain, GEX sign, flip).

**Phase 5: Streamlit**
Pages 1–8, freshness badges, fragments with auto-refresh, Data Health page.
✅ Accept: the app runs locally, auto-refreshes, and shows correct as-of labels; killing a source turns its badge red without breaking other pages.

**Phase 6: scheduling & hardening**
`register_tasks.ps1`, morning retry, log rotation, README runbook ("what to do when X turns red").
✅ Accept: a week of unattended runs at work with the archive growing daily.

**Phase 7 (later): extras**
Composite stress score, Bloomberg adapter, intraday P/C curve page, FastAPI export, holdings-aware event alerts.

---

## 13. Compliance & usage notes (resolve before production use)
- Cboe and OCC website data is provided "as is" under their website terms. Confirm with compliance that **internal, non-redistributed** use in a work dashboard is acceptable, especially for the **undocumented Cboe delayed-quote JSON**.
- CFTC, Treasury and FRED are public government data. FRED series may carry third-party copyright (e.g. some ICE BofA series); fine for internal viewing, but don't redistribute.
- FINRA Reg SHO stays disabled until compliance reviews FINRA's terms.
- No data leaves the work environment; `data/` is never committed.

---

## 14. Rules for the AI assistant building this
1. **Never invent** an endpoint, parameter, column or dataset ID. If this spec's value fails, write or extend a probe, show me the real response (status, headers, first lines), and propose a spec update.
2. Build **one phase at a time**, then stop for review.
3. Write parsers **against recorded fixtures**, and add the fixture in the same change.
4. Keep sources isolated. One source's failure must never break another's run.
5. Prefer simple, readable pandas and DuckDB over clever abstractions. Use type hints and docstrings that say *what the number means* in market terms.
6. All thresholds and windows come from config, not literals.
7. No secrets in code. The FRED key comes from the env var.
8. Label heuristics as heuristics in code comments **and** the UI (GEX sign convention, regime thresholds).
9. Every run must be safe to repeat and must never re-download data already stored as ok.
10. When finishing a phase, update `README.md` with how to run it and what's known to be fragile.

---

## 15. Open questions (I'll answer these as we go)
- Which underlyings beyond the core five? Can a local holdings CSV be produced from the exposure platform?
- Does the existing dashboard read files (CSV/Parquet), a database, or an API? This decides the Section 9.4 export format.
- Will Streamlit run on my desktop only, or on a shared internal server?
- Is any of this going into a risk or limit process, or is it context only? This decides how strict alerting needs to be.
- Phase 0 result: which domains does the corporate network allow? *(Personal machine, 2026-10-29: all hosts in 5.0 reachable, all enabled probes PASS. Work machine: pending.)*
