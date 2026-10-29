# Test fixtures

Real responses from **public** endpoints, recorded on the personal machine (spec C3: never
work data). Trimmed to stay small. Bytes are kept exactly as served (see `.gitattributes`),
including CRLF line endings and trailing commas.

| File | Source URL | Recorded | Trimming |
|---|---|---|---|
| `occ/daily_volume_statistics_20261028.csv` | `https://marketdata.theocc.com/daily-volume-statistics?reportDate=20261028&format=csv` | 2026-10-29 | none |
| `cboe_indices/VIX_History_trimmed.csv` | `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv` | 2026-10-29 | header + first 3 + last 3 rows |
| `cboe_chains/_VIX_trimmed.json` | `https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json` | 2026-10-29 16:27 ET | first 3 of 1520 contracts; re-indented |
| `cfe_futures/settlement.csv` | `https://www-api.cboe.com/us/futures/market_statistics/settlement/csv` | 2026-10-29 | none |
| `cftc_cot/gpe5-46if_limit1.json` | `https://publicreporting.cftc.gov/resource/gpe5-46if.json?$limit=1` | 2026-10-29 | none (one row, unordered) |
| `treasury/daily_treasury_yield_curve_2026_trimmed.csv` | Treasury `daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve...` | 2026-10-29 | header + 5 newest rows |
| `fred/fredgraph_VIXCLS_trimmed.csv` | `https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS` | 2026-10-29 | header + first 3 + last 3 rows |
| `occ/cont_volume_20261028_trimmed.txt` | `…/cont-volume-download?reportDate=20261028&format=txt` | 2026-10-29 | header + 28 of 170,006 lines (A, SPX, VIX, VX, SPY rows covering all 4 product kinds), CRLF kept |
| `occ/cont_volume_not_available.txt` | same URL for an unpublished date | 2026-10-29 | none (the whole 30-byte reply) |
| `occ/volume_query_SPY_20261028.csv` | `…/volume-query?reportDate=20261028&…&symbol=SPY&…&porc=BOTH` (no `contractDt`) | 2026-10-29 | none |
| `occ/weekly_etf_20261023.csv`, `occ/weekly_index_20261023.csv` | `…/weekly-volume-reports?reportDate=20261023&reportType=options&reportClass={etf,index}&format=csv` | 2026-10-29 | none |
| `occ/weekly_not_available.txt` | weekly URL with a non-Friday date | 2026-10-29 | none (the whole 24-byte reply) |
| `cboe_daily/daily_2026-10-28_tables.html` | `https://www.cboe.com/markets/us/options/market-statistics/daily?dt=2026-10-28` | 2026-10-29 | first 8 `<table>` elements verbatim, rest of the 450 KB page dropped |
| `symbol_directory/{nasdaqlisted,otherlisted}_trimmed.txt` | `https://www.nasdaqtrader.com/dynamic/SymDir/{nasdaqlisted,otherlisted}.txt` | 2026-10-29 | header + 4 rows (ETF, stock, test issue, `BRK.B`) + footer |

To record new ones: `omi probe --save-samples <folder outside the repo>`, then trim and copy
the file here and add a row to this table.
