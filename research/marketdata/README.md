# Real-data inputs for the cooldown / async-chunk study

> **Packaged copy** (`research/marketdata/`). Paths in this file are relative to this folder. The chain snapshot and the Nasdaq daily file live in `econ/`. `raw/` (the untouched downloads) and `partial_backup/` are **not** in the repository; `scripts/refresh.sh` re-downloads and rebuilds into a new folder.

Prepared on 2026-10-21 between 15:00 and 15:15 ET, during the regular session. Nothing in the repository was touched.
Every derived file can be rebuilt from `raw/` with `scripts/build_bars.py` and `scripts/build_options.py`.

## Stock paths: what was downloaded

| Source | What it gives | Status |
|---|---|---|
| Yahoo chart API (`query1/query2.finance.yahoo.com/v8/finance/chart`, 1m/7d, 2m/60d, 5m/60d) | Not obtained | **HTTP 429 from this egress** on every try, including the cookie and crumb route. A backoff loop (`scripts/yf_retry.sh`, log in `raw/yf_retry.log`) ran during the build and never got a 200. |
| **WSJ/MarketWatch charting API** (`api.wsj.net/api/michelangelo/timeseries/history`, public ckey) | 1-minute OHLCV for the **last 10 sessions**, which is the API's limit for intraday steps (D15, D20, P1M and similar are rejected). Daily OHLCV for 1 year. | **Used** as the primary source. |
| CBOE delayed intraday (`cdn.cboe.com/.../charts/intraday/<SYM>.json`) | 1-minute OHLC for the current session only | Cross-check only |
| Nasdaq `api.nasdaq.com/.../chart` | Today's 1-minute prices | Raw file kept, not used |

Stooq, Barchart and CNBC were also tried for a longer intraday set. All of them are blocked from here (connection reset or 403).
**So there is no intraday set longer than 10 sessions.** The longer set is daily only (`daily_1y.csv`).

### Sessions covered (1-minute bars, regular session 09:30 to 15:59 ET, bars labelled by their start)

* Complete sessions: **2026-10-08, 10-09, 10-12, 10-13, 10-14, 10-15, 10-16, 10-19, 10-20**. Each has 390 bars per symbol plus the official 16:00 closing-auction print.
* **2026-10-21** was first pulled during the session (09:30 to 15:03 ET, 334 bars) and then refreshed: the packaged files hold **389 bars (09:30 to 15:58 ET; the 15:59 bar is missing) plus the official 16:00 close** (see the note at the end). `scripts/refresh.sh [OUT_DIR]` re-pulls everything and rebuilds into a **new** directory, so the shared files stay untouched. Run it after about 16:05 ET to get the full Oct-21 session (SPCX moved +8.6% at the open that day, with 67% realized vol).
* **Missing minutes: 0** for SPCX, RKLB and SPY in every complete session; 2026-10-21 lacks only its 15:59 bar (all three symbols). No zero-volume bars either (`bars_1m_gaps.csv`). The three symbols align minute for minute, and `*_filled` is 0 everywhere.
* Cross-checks:
  * WSJ against CBOE on 319 common Oct-21 minutes: the closes are identical (median difference 0, maximum 4e-4 relative) and so are the volumes. This also confirms the labelling: WSJ labels a bar by its start, CBOE by its end.
  * WSJ daily close against the official 16:00 tick: identical.
  * WSJ daily against `econ/daily.csv` (Nasdaq): identical, except one SPY day (2026-04-20, a difference of $1.42).

### Market context in the window

Moves were large over these 10 sessions:
* RKLB fell from 70.5 to 50.9 (−7.8%, −6.6% and −8.0% on Oct 14 to 16; 1-minute realized vol of 100% on Oct 15).
* SPCX fell from 167 to 147.8, then opened at 160.5 on Oct 21.
* SPY traded between 761 and 779.

Before you replay a past session against **today's** strikes, decide whether you want absolute prices or relative moves (see `replay_loader.spot_path(start_spot=...)`). Absolute prices would put RKLB at 70 against 45P/55P.

How far prices move intraday (`move_stats.txt`), as the median, 90th and 99th percentile of |log move| in %:

| | 1 min | 15 min (one option cooldown) | 60 min |
|---|---|---|---|
| SPCX | 0.06 / 0.20 / 0.47 | 0.25 / 0.76 / 1.77 | 0.46 / 1.32 / 2.75 |
| RKLB | 0.08 / 0.24 / 0.62 | 0.28 / 0.89 / 2.12 | 0.54 / 1.81 / 4.42 |
| SPY | 0.01 / 0.04 / 0.08 | 0.05 / 0.15 / 0.32 | 0.11 / 0.31 / 0.62 |

The closing auction differs from the last 1-minute close by a median of 3.2 bp for SPCX (10.8 bp at most), 1.5 bp for RKLB and 0.5 bp for SPY. Scoring uses the official close (`official_close` in `sessions_summary.csv`), not the 15:59 bar.

## Option inputs: snapshot timing and spot

### The chain snapshot

`econ/chains.csv` is a CBOE delayed chain pulled at 2026-10-20 21:10 UTC (17:10 ET). Its option bid and ask are the **Oct-20 closing quotes**.

Its `under` column is an **after-hours stock print** (SPCX 153.67, RKLB 52.05, SPY 771.04), not the spot those quotes reflect. Put-call parity on the 5 strikes nearest the money recovers the spot the quotes do reflect:

| | Parity spot | Official Oct-20 close |
|---|---|---|
| SPCX | 147.84 to 147.86 | 147.79 |
| RKLB | 50.90 (Nov20) | 50.90 |
| SPY | 769.86 | 769.05 (SPY options trade to 16:15) |

The chain is therefore valued at the **median parity spot**, at 16:00 ET on Oct-20. CBOE's own `iv` column agrees with the app-engine IV at that spot, for example SPCX Nov27 145C at 0.5528 against 0.5516.

### Today's quotes

Today's quotes come from the operator monitor at **2026-10-21 13:13 ET**, the same 17-contract table as `MONITOR` in `research/frontier/calib.py`. That covers all 7 mandated legs and all 10 option hedges. The spots were SPCX 160.30, RKLB 53.24 and SPY 773.85, and they match the 13:13 one-minute bars.

### Pricing

All IVs and greeks come from the app's own `hedge.valuation.ValuationEngine`: American exercise, Bjerksund-Stensland, r = 0.04 and no dividends, as in `config/contest.template.yaml`.
* Vega is per 1.00 of vol, not per vol point.
* Theta is per calendar day.

### IV scaling

1. For each (symbol, expiry), a quadratic smile in ln(K/S) is fitted to the chain's engine IVs. It uses the out-of-the-money side plus everything within 2.5% of the money, weighted by 1/spread. The fit error (RMSE) is 0.0006 for SPY, 0.0015 to 0.007 for SPCX and 0.024 to 0.030 for RKLB, whose Nov27 chain quotes are wide.
2. Ratio = today's IV ÷ the chain smile at today's moneyness (sticky moneyness), taking the median over the quoted contracts:

   | | Nov13 | Nov20 | Nov27 |
   |---|---|---|---|
   | SPCX | 0.917 (no quote today; Nov20 ratio borrowed) | 0.917 | 0.935 |
   | RKLB | – | 0.972 | 0.969 |
   | SPY | 0.986 | 0.991 | 0.991 |

   SPCX implied vol came down after the up-gap.
3. `iv_sim` is today's exact inverted IV when the contract was quoted today, which reproduces today's mid. Otherwise it is the chain smile × the ratio.

### Spreads

`rel_spread_sim` is today's observed (ask − bid)/mid when the contract was quoted today. Otherwise it is the chain's own Oct-20 relative spread. Both are kept in the file (`chain_rel_spread`, `today_rel_spread`) so a study can pick either.

Today's spreads are often wider than the chain's:

| Contract | Today | Chain |
|---|---|---|
| RKLB Nov27 50C | 14.7% (6.30/7.30) | 11.5% |
| SPCX Nov27 145C | 6.7% (19.50/20.85) | 3.7% |
| SPCX Nov20 145C (mandated short) | 3.9% | 1.3% |
| SPY Nov27 770C | 1.1% | 0.4% |

Some chain-only RKLB and SPCX Nov27 strikes have very wide quotes, up to 40 to 60% relative, for example RKLB Nov27 51P at 3.45/6.15. These are real snapshot quotes but may be stale end-of-day NBBO.

## Files

| File | Contents |
|---|---|
| `bars_1m.csv` | Tidy 1-minute bars with columns `timestamp_et` (ISO with offset), `date`, `symbol`, `open`, `high`, `low`, `close`, `volume`. 09:30 to 15:59 ET, 10 sessions (the last one partial). |
| `bars_1m_aligned_close.csv` | A wide minute grid. It has the close for each symbol and a `*_filled` flag (all 0). It also has `*_rel_open` (close ÷ session open) and `*_rel_prevclose` (close ÷ previous official close, which includes the overnight gap), for replaying real moves from any starting spot. |
| `bars_1m_gaps.csv` | Expected, present and missing minutes, zero-volume bars and flat bars, per session and symbol. |
| `sessions_summary.csv` | Per session and symbol: open, high, low, last 1-minute close, **official close**, 1-minute realized vol (annualized), close-to-close return and volume. |
| `daily_1y.csv` | WSJ daily OHLCV for 1 year. SPCX starts at its listing on 2026-06-12. `partial=1` marks the still-open Oct-21 row. |
| `move_stats.txt`, `bars_report.txt` | Move distributions; build log and cross-checks. |
| `options_chain_subset.csv` | Raw chain rows for SPCX Nov13/20/27 (125 to 175), RKLB Nov20/27 (40 to 60) and SPY Nov13/20/27 (760 to 785), both rights. Columns include bid, ask, mid, spread, rel_spread, CBOE iv and greeks, open interest, volume, the after-hours `under_file`, `chain_spot` (parity) and `iv_app_chain`. |
| `options_today_quotes.csv` | The 17 contracts quoted at 13:13 ET: bid, ask, relative spread, `iv_today`, the ratio to the chain smile, the chain bid/ask/IV at the same strike, and engine greeks at the 13:13 spot. |
| `options_contracts.csv` | **Simulator-ready table, one row per contract (344 rows).** See the column notes below. |
| `options_smiles.json` | Smile coefficients and the ratio for each (symbol, expiry), plus the chain and today spots and the parity spots. Use these for sticky-moneyness IVs: iv(x) = polyval(coef, x) × ratio_today, with x = ln(K/S). |
| `options_report.txt` | Parity, ratios, smile fits, today against chain, and the key-contract table. |
| `replay_loader.py` | Optional helper, described below. |
| `scripts/` | `wsj_fetch.sh`, `build_bars.py`, `build_options.py`, `refresh.sh` and `yf_retry.sh`. Run the Python scripts with `python -I`, because the inputs are untrusted downloads. |
| `raw/` | Untouched downloaded JSON: WSJ 1-minute and daily, CBOE intraday, Nasdaq SPY. **Not packaged.** |
| `econ/chains.csv` | CBOE delayed option chains (SPCX, RKLB, SPY and peers), pulled 2026-10-20 21:10 UTC. |
| `econ/daily.csv` | Nasdaq daily closes and volumes for the mandated names and their peers (about 1 year). |
| `econ/cboe/VIX*_History.csv` | CBOE VIX9D, VIX, VIX3M and VIX6M daily history (used by `research/econ/`). |

Column notes for `options_contracts.csv`:
* `role`: `mandated` (7, with `mandated_qty`), `hedge` (the 10 option hedges in the app universe, `in_app_hedge_universe=1`), `nearby_requested` (the requested strike windows), or `nearby_extra`.
* `contest_tradable`: 0 for the forbidden Nov20 RKLB calls, the other Nov20 SPY options and the mandated legs.
* IV: `iv_sim` and `iv_sim_source`.
* Spread: `rel_spread_sim` and `rel_spread_sim_source`.
* At today's 13:13 spot: `model_mid_today`, `delta_today`, `gamma_today`, `vega_today` and `theta_today`.

### `replay_loader.py` (optional)

* `spot_path(date, start_spot=None, bridge=False, seed=0)` returns per-second spots from 09:30:00 to 16:00:00 ET (23,401 points).
  * Each minute's open is pinned at :00 and its close at :59, with linear interpolation between them.
  * `bridge=True` adds a seeded Brownian bridge sized to the bar's high-low range.
  * `start_spot={...}` replays the session's relative moves from that spot.
  * For a partial session the path stays flat after its last bar.
* `option_inputs()` returns, per (symbol, 'YYYYMMDD', strike, right), the `iv`, `rel_spread`, role and flags.
* `quote_from_price(price, rel_spread)` returns a symmetric bid/ask rounded to the tick.
* `official_close(date)` and `sessions()`.

## Caveats

* **The option prices are modeled. There are no real intraday option prints.** Simulated quotes are ValuationEngine prices on the real stock paths at a fixed per-contract IV (sticky strike, unless you use `options_smiles.json`), with a constant **relative** spread taken from one snapshot. Real IV moves with spot (SPCX IV fell about 8% on the +8.6% gap), and real spreads widen at the open, near the close and on fast moves. A constant-IV replay therefore understates P&L from vol and spread-crossing in stressed minutes.
* **One snapshot each:** the chain IVs and spreads are from the Oct-20 close, and today's from one 13:13 ET look. Ratios for SPCX Nov13 are borrowed from Nov20.
* **Paths:** 10 sessions of 1-minute data (9 complete), which is short. RKLB's window contains an unusual sell-off (Oct 14 to 16), so treat its tails as stressed, not typical. There is no intraday set longer than 10 sessions, because Yahoo returned HTTP 429.
* **Within the minute** the path is interpolated (linear, or a bridge). Real moves inside a minute and bid/ask bounce are not in the data. Simulated stock spreads must be added by the simulator (SPCX/RKLB about 1 to 2 cents, SPY 1 cent in the existing sim).
* **WSJ prices are consolidated last trades**, not NBBO mids. Volumes are consolidated volume.
* **Closing auction:** the official close can differ from the 15:59 bar by up to 10.8 bp for SPCX. Use `official_close` for scored closes.
Note: Oct 21 refreshed to 15:58 (389 bars) on Wed Oct 21 23:52:51 UTC 2026 from data_full/; previous partial files in partial_backup/ (neither folder is packaged).
