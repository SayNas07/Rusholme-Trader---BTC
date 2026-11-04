# Rusholme Trader – BTC

Catalyst watchlist screener built on the Bloomberg Terminal API (`xbbg`). It pulls a snapshot and one year of prices for a watchlist, computes returns, valuation and cash-runway metrics, and flags names that fit a "PRAX-style" setup: a non-penny stock, a $0.5–5B market cap, a dated catalyst, no big run-up yet, and enough cash to avoid dilution.

> Research tool only. Not investment advice.

## Requirements

- A Windows PC with a **Bloomberg Terminal logged in** (the API talks to the local terminal session)
- Python 3.12
- Packages:

```
pip install --user blpapi --index-url=https://blpapi.bloomberg.com/repository/releases/python/simple/
pip install --user "xbbg[pandas]" openpyxl
```

`openpyxl` is only needed to read EQS `.xlsx` exports.

## Files

| File | Purpose |
|---|---|
| `main.py` | Connection test. Fetches `PX_LAST` and `CUR_MKT_CAP` for four tickers and writes `watchlist.csv`. |
| `step2.py` | Main analysis. Fetches the snapshot, price history and metrics, then writes the CSVs below. |
| `earnings_scan.py` | Ranks tickers reporting in a given week by how likely a positive earnings reaction is. See [Earnings scan](#earnings-scan). |

## Usage

Connection test:

```
python main.py
```

Full analysis on the built-in watchlist:

```
python step2.py
```

Add the tickers from a Bloomberg EQS screen export (any `.xlsx` with tickers like `XXXX US Equity` in the first column):

```
python step2.py XML5070R.xlsx
```

### Output

| File | Contents |
|---|---|
| `watchlist_snapshot.csv` | Raw Bloomberg fields, one row per ticker |
| `watchlist_prices.csv` | Daily `PX_LAST` for roughly the last year |
| `watchlist_summary.csv` | Snapshot plus derived metrics and flags, grouped in the console output |
| `watchlist_candidates.csv` | Only names flagged `PASSES FILTER`, sorted by upside to analyst target |

### Derived metrics

- `ret_1m`, `ret_3m`, `ret_6m`, `ret_1y`: price returns over 30, 91, 182 and 365 days
- `off_52w_high`: price versus the 52-week high
- `upside_to_target`: Bloomberg consensus target versus price
- `cash_pct_mktcap`: cash plus short-term investments as a share of market cap
- `runway_months`: cash plus short-term investments divided by trailing-twelve-month free cash flow burn (blank if free cash flow is positive)

### Flags

| Flag | Meaning |
|---|---|
| `PENNY<$5` | Price under $5. (Meaningless for bank ADRs.) |
| `MICROCAP` / `SWEET-SPOT cap` / `LARGE-2x unlikely` | Market cap under $0.5B, $0.5–5B, over $20B |
| `ALREADY RAN >50% 3M` | Up more than 50% in three months |
| `CASH-RICH` | Cash and short-term investments above 50% of market cap |
| `DILUTION RISK<12m` | Less than 12 months of cash runway |
| `PASSES FILTER` | Price ≥ $5, cap $0.5–5B, 3-month return under 50%, no dilution flag |

`PASSES FILTER` does not check for catalysts. The catalyst dates in `step2.py` are entered by hand and need checking against company releases, FDA calendars and SEC filings.

## Earnings scan

```
python earnings_scan.py
python earnings_scan.py --tickers PRAX,AMAT --start 2026-11-09 --end 2026-11-12
```

Pulls a snapshot (price, cap, short interest, implied vol, put/call), 4-week estimate revisions, 1-year price momentum, and the last 8 earnings reactions for each ticker, then writes `earnings_scan.csv` and prints a ranked table plus a short note on the top 5.

Score (each component shows in the `signals` column): +1 for EPS revisions > 0, sales revisions > 0, post-earnings up-rate > 60%, EPS beat-rate > 70%, days to cover > 5, put/call < 1; -1 for a 1-month return > 25% and for an expected move < 8%. Blank inputs score 0, and `signals_have` in the CSV shows how many of the 8 inputs were available.

- **Reaction timing:** after-close reports use day 0 to day +1, before-open and intraday reports use day -1 to day 0, and unknown timing uses day -1 to day +1.
- **Expected move:** the larger of an IV-based proxy (`0.8 x 30-day IV x sqrt(7/365)`) and the average absolute past move. The proxy usually understates a true earnings-straddle price.
- **Field names:** each field has candidate mnemonics and the run prints which one worked. For any that come back `UNRESOLVED`, look the field up with `FLDS` on the Terminal.
- **Window:** tickers whose `EXPECTED_REPORT_DT` is outside `--start/--end` are ranked last with rank `-`.

## Editing the watchlist

The `WATCH` dictionary near the top of `step2.py` maps each ticker to `(group, catalyst)`. Add or remove entries there. The snapshot fields are in the `FIELDS` dictionary. Each field is fetched on its own, so a bad field name is skipped rather than breaking the run.

## Gotchas

- **Console encoding:** the Windows console (cp1252) can't print xbbg's table characters, so both scripts call `sys.stdout.reconfigure(encoding="utf-8")`.
- **Narwhals/Arrow output:** new xbbg versions return Narwhals/Arrow tables instead of pandas. The scripts pass `backend="pandas"` and convert with the `to_pd()` helper.
- **Long format:** `bdp` and `bdh` can return long tables (`ticker, field, value`). The scripts pivot them to wide.
- **Units:** `BS_CASH_*` and `CF_FREE_CASH_FLOW` come back in millions of reporting currency, while `CUR_MKT_CAP` is in USD. The scripts convert before comparing them.
- **Cash field:** `BS_CASH_NEAR_CASH_ITEM` leaves out short-term investments, which understates biotech cash. `BS_CASH_CASH_EQUIVALENTS_AND_STI` is used when available. If it comes back blank, look it up with `FLDS` on the terminal.

## License

MIT. See [LICENSE](LICENSE).
