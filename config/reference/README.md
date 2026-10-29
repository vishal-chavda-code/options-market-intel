# Reference data (committed)

Small lookup tables the pipeline joins against. Edit them here; they contain no market data.

| File | What | Where it came from |
|---|---|---|
| `occ_exchanges.csv` | OCC one-letter exchange code → exchange name | `exchangeId`/`exchangeName` pairs in OCC `trade-volume-download` XML for 2026-10-28 |
| `index_underlyings.csv` | Index-option underlyings → broad / narrow scope and family | Hand-maintained. **Narrow vs broad is not published in any free feed found so far.** `confidence` says how sure the entry is. Rows marked `unclassified` or `low` need confirming (compliance or the exchange's product spec). Index underlyings missing from this file show up as `unclassified` in the dashboard. |

ETF vs single stock is **not** maintained here. It comes from the Nasdaq Trader symbol directory (`symbol_directory` source), which carries an ETF Y/N flag for every US listing.
