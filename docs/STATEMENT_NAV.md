# The scored close: IBKR Activity Statement NAV

The instructor scores each day on the IBKR Activity Statement's **Net Asset Value, Ending
Value** (confirmed Oct 23). That replaces the Oct 21 working assumption that the API closing
NetLiquidation was official, and the Oct 22 one that the NAV after the 20:00 ET post-market
close was. Implementation: `hedge/statement.py`.

## IBKR's definition

- **NAV:** on statements, NAV "represents Net Liquidation Value as of the date indicated in the
  report" (IBKR glossary).
- **Ending NAV:** the statement's NAV section shows one row per asset class plus rows for interest
  accruals and dividend accruals, each valued "at the close of the period covered by the statement"
  (IBKR Reporting Reference, Net Asset Value). For this account that is Ending NAV = Cash + Stock +
  Options + Interest Accruals (+ Dividend Accruals: none expected, as SPCX and RKLB pay none and
  IVV/VOO go ex-dividend in December).
- **Change in NAV:** mark-to-market (position MTM and transaction MTM), interest (including accruals
  and reversals), commissions, transaction fees, dividends, deposits and withdrawals, and other items.
- **Interest accruals** (IBKR interest calculation page):
  - **Balance:** interest is calculated daily on **settled** cash above USD 10,000.
  - **Rate:** the benchmark minus 0.5% on IBKR Pro (3.38% on Oct 23) or minus 1.5% on IBKR Lite.
  - **Arithmetic:** a 360-day year for USD, rounded to the cent per tier.
  - **Posting:** each day's interest is added to an Accrued Cash balance. Early in the next month
    IBKR reverses that balance and books the final interest to cash, which leaves NAV unchanged up
    to small recalculation differences. Expect this around Nov 2-4.
  - **What the app sees:** IBKR's API AccruedCash stayed at 1,601.06 from the Oct 22 statement
    through Oct 23 evening. The app learns the statement's extra accrual from each imported
    statement, and learns Fridays separately, since a Friday statement may carry the weekend.
- **Timing:** "The reporting window closes at ... 8:20PM EST for securities. Statements will be
  available around midnight (EST)" (IBKR Web API docs). The Account Management API that sentence
  comes from is for brokers and advisors and returns PDFs; individual accounts automate statements
  through the Flex Web Service.

## When it is taken

It is not one snapshot. Each part has its own time (IBKR FAQs; Oct 22 numbers below):

- **Cash and accruals:** every trade with trade date T. Post-market trades up to 20:00 ET carry
  date T; overnight-session trades from 20:00 carry T+1. IBKR closes the securities statement
  window at about 20:20 ET, and the daily statement appears by about 05:00 ET the next morning
  (documented for live accounts; paper accounts are not mentioned).
- **Shares and ETFs:** the regular-session close at 16:00 ET (IBKR's source: Telekurs). After-hours
  prices are never used.
- **Options:** OCC's once-daily closing price. OCC starts from the closing NBBO midpoint across
  exchanges and smooths implied volatility per class and expiry. Cboe says OCC uses the 16:00 NBBO
  "in all cases", and the Oct 22–23 statements confirm it for SPY options too, although they trade
  until 16:15 (see "Option closing prices: real-data check" below).
- **Rounding:** the statement keeps the 4-decimal close (9.0497, 18.4774). Rounding the closes to
  cents does not reproduce the Oct 22 total; the unrounded closes do.

## What the statement NAV is

The Oct 22 statement reconciles to the cent:

| Component | Statement | Where it comes from |
|---|---|---|
| Cash | 872,744.50 | End-of-day cash; post-market trades (to 19:47) included. Equal to the API TotalCashValue after 20:00 |
| Interest accruals | 1,601.06 | API AccruedCash |
| Stock | 74,591.65 | RKLB 1,120 x **51.22** + SPCX 119 x **144.75**: the **regular-session (16:00) close**, IBKR's RTH daily TRADES bar. The after-hours marks gave 74,302.55 |
| Options | 34,437.00 | Each leg at OCC's end-of-day price from the **16:00 NBBO**, mostly its midpoint (SPY options too) |
| **Total** | **983,374.21** | |

Option marks against our captured books:
- RKLB 45P 1.93 and SPCX 160C 3.70: the 16:00 midpoint, exact.
- SPCX 145C 9.0497: the 16:00 midpoint, 9.05.
- SPY legs (1120C 10.74, 1120P 14.415, 1127C 12.115, 1127P 15.24): within 5 cents of the app's
  15:59:52 midpoints, calls slightly above and puts slightly below (a put-call parity fit), and
  10–18 cents from the 16:15 midpoints. At first read as 16:15; Oct 23 settled it (below).
- SPCX 130C 18.4774: inside the spread, near the midpoint. Non-round, so possibly another feed or
  instant.
- RKLB 55P long 6.55: the **bid** of a wide 6.55/7.40 book, $1,062.50 below the midpoint. One
  observation; treat a wide-spread leg as uncertain within its bid/ask.

The API NetLiquidation after the post-market close was 983,514.38. It used after-hours share prices
and model-style option marks, so it was $140.17 off the statement.

## Consequences in the app

- **Market risk ends at 16:00.** `Manifest.scoring_close` is the regular close again. The forecast
  runs to 16:00 (`next_close`), and the overnight term covers 16:00 to the next open.
- **Trades still book until 20:00.** `Manifest.trading_end` is the end of the post-market session.
  A trade after the close changes the day's NAV by quantity x (mark - price) - fees, which is a
  cost, never a hedge.
  - The hedger therefore no longer trades post-market. `can_trade` allows it only with
    `post_market=True`, which only the closing burner and its risk checks pass.
  - Pre-market hedging (04:00-09:30) continues: those trades book to that day, marked at its
    close.
- **Predicted statement NAV** (`TradingService.statement_estimate`, from 16:16 ET):
  - **Base:** a settled IBKR cash push (no fill 20 s before it or since) plus accrued interest plus
    every position at its statement mark.
  - **After the base:** later fills roll it forward exactly.
  - **Share marks:** IBKR's RTH daily bar close, fetched at 16:05 and 16:30 (`official_closes`).
  - **Option marks:** from the `pre_close` capture (last before 16:00) for every option, SPY's
    included; `options_close` (16:15) only when nothing else exists.
  - **Uncertainty:** each option mark is uncertain within its closing bid/ask. `low`/`high` put
    every mark at its least/most favorable side; `samples` draw them uniformly.
- **Dashboard:** after 16:16 the ACCOUNT NAV card and the provisional daily score use the
  predicted statement NAV (nav_feed "predicted statement NAV (closing marks)").
- **Recorder:** at 20:05 ET the end-of-day reading records the predicted statement NAV as the
  day's provisional close (an "IBKR API ..." row). If the marks are missing, it records the API
  NetLiquidation instead and says so. The imported statement always supersedes it, and the app
  asks for it before the next open.
- **Closing burner:** in the contest it works after the close, from 16:16 until the post-market
  cutoffs before 20:00, shares only.
  - **Gap:** predicted statement NAV minus target. It is uncertain only by the option prices.
    With `post_close_basis: worst_case` (the code default) the burner spends only `low` minus
    target, the gap with every option at the least favorable side of its closing bid/ask. With
    `quantile` (the contest's setting from Oct 26) it spends toward `spend_quantile` of the samples
    around the predicted prices.
  - **No just-in-time wait:** the gap no longer moves with the market.
  - **Cost per round trip:** exactly the spread paid plus commissions.
- **Official close:** `api_close_official` is False. An API row is provisional and warns until the
  statement is imported.

## Automatic statement import, fallbacks and calibration (Oct 24)

- **Nightly import** (`hedge/flex_import.py`):
  - **When:** from 00:10 ET after each session the app asks the Flex Web Service for that day's
    statement only. If it is not ready, it asks again every 30 minutes, and Day 0 and missed days are
    backfilled.
  - **What it records:** the NAV becomes the official close (a statement row) and the position
    prices are stored. One-time setup: Setup IBKR Flex.command.
- **Missed closing capture** (`service._fallback_close_marks`): when the app has no 16:00/16:15
  option capture, for example because the Mac slept, it takes the Moomoo collector's archived book
  from the last 10 minutes before each option's close, else Moomoo's final book after the close.
- **Option closing prices** (`hedge/occ_marks.py`, `hedge/marks.py`):
  - **Snapshot:** at 15:55 the app lists the held options' chains (strikes within ±30%), then
    quotes them at 15:59:57, SPY's included, and SPY-style chains again at 16:14:57
    (`ops/fetch_moomoo_chain.py`, quote-only).
  - **Price:** right after each snapshot the app rebuilds the Options Clearing Corporation's
    end-of-day price from that chain, once, in a thread (an `occ_marks` record). Each held
    option's statement mark is that price, else the local smoothed-IV price, else the midpoint,
    always inside its closing bid/ask. The mark's `source` says which.
  - **Scoring:** the Statement prices check scores every estimator against each statement:
    midpoint, local smoothing, OCC rebuild, each option at its worst bid/ask side, and IBKR's own
    marks at 19:56 and at the end of the evening (`evening_marks`, every 3 minutes 16:00–01:00).
  - **Tuning:** `ops/mark_backtest.py` scores the estimators on every past statement
    (`ops/statement_history.py` fetches up to 45 days through Flex). PARAMS in
    `hedge/occ_marks.py` change only after that review.
- **Statement prices check:** each statement's own prices are compared with the predicted marks.
  They come from the nightly import or from Open Positions pasted in Settings & alerts.
- **Statement-style value:** before 16:16 the dashboard also shows the account valued at live
  midpoints, next to IBKR's NAV, which uses IBKR's own marks.
- **Day-end report:** each close is valued at the statement's prices once imported, else at the
  predicted statement marks.
- **Power:** the Today panel has a Power row, and from 90 minutes before a scored close the app
  alerts when the Mac is on battery or its lid is closed.

## Each morning

With the Flex import set up, nothing: the statement NAV is recorded by itself after midnight. Without
it, enter the previous day's statement Ending Value in Settings & alerts > Import an official
closing NAV. The Previous official close row warns until you do. Trading still starts at 04:00 on
the predicted value.

## Option closing prices: real-data check (Oct 24)

Truth: the Oct 22 and Oct 23 statements' option prices (Moomoo's prev_close_price equals them for all
ten Oct 22 legs) and Oct 22's prev_close for the other recorded strikes. Error = size x (estimate -
statement), summed per leg as |error|.

| Day, inputs | Midpoint | hedge/marks.py smoothing | OCC rebuild |
|---|---|---|---|
| Oct 23, SPCX/RKLB legs, full final chain (Moomoo, 2,912 codes) | $526 | $367 | $1,458 (vega weights), **$227** (spread weights) |
| Oct 22, all legs, app's 15:59:52 books | $1,485 (RKLB 55P: $1,062) | $1,550 (books from mixed seconds) | n/a: 1–3 strikes per expiry recorded |
| SPY legs at 16:00 vs 16:15 books | Oct 22: $350 vs $1,197; Oct 23: $588 (15:56) vs $3,292 | | |

Decisions: SPY options are priced from their 16:00 book like every other class; the rebuild weighs
each quote by 1/spread² (a tight quote stays at its midpoint, a wide one moves toward its chain).

### With IBKR's own 16:00 books (Oct 24 evening)

IBKR's historical data (ops/fetch_ibkr_closing_books.py: the 15:59 one-minute BID and ASK bar
closes, the quotes standing at 16:00) against every statement leg from Oct 8 and Oct 22's OCC prices
(185 contracts with both so far; the run continues overnight):

| | Midpoint | hedge/marks.py smoothing | Rebuild, fitted forward | Rebuild, per-class forward |
|---|---|---|---|---|
| Mean \|error\| per contract | $0.089 | $0.084 | $0.082 | **$0.062** |
| SPY / SPCX / RKLB / BA | 0.107 / 0.031 / 0.123 / 0.009 | 0.096 / 0.034 / 0.121 / 0.021 | 0.094 / 0.019 / 0.126 / 0.038 | 0.070 / 0.015 / 0.096 / 0.039 |
| Held legs' value error Oct 21 / 22 / 23 | $760 / $1,054 / $199 | $917 / $347 / -$39 | $792 / $468 / $168 | $892 / $4 / $77 |

- Tight quotes on single stocks are priced at their midpoint (BA: all nine at the midpoint; SPCX's
  tight legs to the cent); a wide one moves along its chain (RKLB 55P at its bid on Oct 21 and 22).
- Each single-stock expiry's OCC prices satisfy put-call parity with one forward, the at-the-money
  pair's (RKLB Nov 20: 51.58 from every strike; the 50-strike midpoints 51.63; the five-strike
  median 51.40), so the rebuild now fixes it there ("forward": "atm").
- SPY's OCC forward sits 0.2-0.3 below its 16:00 quotes' parity (calls below their bid, puts above
  their ask) at about the share price carried at 3.9%: "forward_late": "spot", "rate": 0.039.
  Our SPY legs are straddles, so this mostly cancels in value.
- The burner's worst-case bound held every day: the statement's options were $1,408-4,829 above
  every option at its least favorable 16:00 side (single SPY legs fell $75-150 outside).
- In the statements the options are $199-1,054 *below* their 16:00 midpoints each day.

### Tuned settings (Oct 25)

2,192 IBKR books, 1,003 contracts with an OCC price. The model families tried were 267 rebuild
settings, gradient boosting on the rebuild's residual, and a small transformer over the whole chain.
Each was checked by a second, independent run. Neither learned model beat the rebuild once whole days
were held out. Their apparent gains came from learning Oct 22's own SPY forward offset, which flips
sign from day to day. Three rebuild changes did beat it:

- **Joint weight scale.** One weight scale for a strike's call and put (`weight_norm: joint`), so a
  tight quote keeps its midpoint against a wide one on the other side.
- **Single-stock forward.** The parity forward interpolated at spot (`forward: interp`).
- **SPY forward.** The at-the-money pair's parity on American midpoints (`forward_late: atm`,
  `forward_space_late: mid`), in place of spot carried at 3.9%. It follows the chain's dividends.

| | Midpoint | Smoothing | Old settings | **New settings** |
|---|---|---|---|---|
| Mean \|error\| per contract | $0.115 | $0.104 | $0.063 | **$0.048** |
| SPY / SPCX / RKLB / BA | 0.126 / 0.036 / 0.141 / 0.009 | 0.117 / 0.026 / 0.095 / 0.021 | 0.070 / 0.014 / 0.067 / 0.039 | 0.054 / 0.011 / 0.049 / 0.013 |
| Held legs' value error, Oct 21 / 22 / 23 | $760 / $1,054 / $199 | $917 / $363 / $195 | $892 / $27 / $95 | **$621 / $35 / $29** |

- **Oct 21:** most of the remaining error is one leg, RKLB 261120P55, priced at its bid.
- **What the gains rest on:**
  - The single-stock gains hold on every day.
  - The SPY gain rests on Oct 22 alone, since on the other days both rules clip to the same book edge.
- **What no 16:00 input explains:** OCC's SPY forward moves by a whole-day shift of +0.25, about
  −0.2 and about −0.3 on Oct 21, 22 and 23. SPY's share price at no minute from 15:55 to 16:20
  (`ops/probe_close_sources.py spy`), and not its official close, matches it.

Most of the evidence is Oct 22. The Oct 23 chain (IBKR books overnight, OCC prices from Moomoo on
Monday morning) is the out-of-sample check; from Oct 26 every chain snapshot also carries the
previous day's price of every listed option.

### More days of OCC prices (Oct 25)

**Where past OCC prices exist:**

- **IBKR:** the statements (held options only) are the only source. IBKR has no daily option
  bars: `probe_close_sources.py ibkr-daily` got error 162 for every one.
- **Moomoo:** every option quote carries the previous trading day's OCC price
  (`prev_close_price`), and the collector archives each watched option's quote.
  - `ops/archive_prev_close.py` turns those archives into a `mark_backtest.py --dataset`. It equals
    the statements on 58 of 58 held legs.
  - It skips weekend archives, whose quotes still carry Thursday's price.
  - It takes a book as the 16:00 book only from 15:58 on: the collector sometimes stopped at
    15:30–15:42.
  - That adds OCC prices for Oct 6–22, about 14 options a day (mostly BA), with 16:00 books on
    Oct 7, 8 and 22. Together with IBKR's books, the rebuild scores $0.047 a contract against
    $0.111 for the midpoint on 1,301 contracts over 13 days, each day held out.

**Moomoo's option ranking (Oct 25)** gives past days' OCC prices for whole chains:
`get_option_rank(trading_date=D)` lists each contract's last trade and its change against the
previous close, which is OCC's price of the day before. `ops/rank_history.py` recovers them for
Sep 14 – Oct 22: about 306,000 prices, 7,000–13,000 a day over SPY, RKLB, SPCX, BA, QQQ and IWM.
Against Oct 22's known prices they are equal on 2,003 of 2,004 traded rows and 91 of 91 untraded
rows. The 16:00 books come from IBKR: `--wanted-chains` picks two expiries a day, the 8 strikes
nearest the money, calls and puts.

**29-day backtest (Oct 25).** 5,212 contracts over 30 days, mean |error| per contract:

| | Midpoint | Oct 24 settings | Before the clip | **Live (clip on)** | 1/spread⁴ + clip |
|---|---|---|---|---|---|
| All | 0.108 | 0.078 | 0.067 | **0.065** | 0.066 |
| RKLB (1,991) | 0.096 | 0.076 | 0.064 | **0.058** | 0.061 |
| SPCX (1,282) | 0.100 | 0.090 | 0.078 | **0.078** | 0.079 |
| SPY (1,757) | 0.129 | 0.075 | 0.068 | **0.068** | 0.068 |
| BA (182) | 0.095 | 0.031 | 0.018 | **0.018** | 0.017 |

- **Days at or below the midpoint:** the live settings on 28 of 30, the Oct 24 settings on 18.
- **Against the Oct 24 settings:** the live settings beat them on 29 of 30 days.
- **Held legs' value error:** Oct 21/22/23 is $30 / $26 / $29 (midpoint $760 / $1,054 / $199).
- **The days before Oct 21** carry partial chains (two expiries, 8 strikes), so their errors are
  higher than on the full chains of Oct 21–22.

**Daily collection, Oct 26 to Nov 5** (read-only; it never touches the app):

1. **About 09:35 ET** (after Moomoo's roll), snapshot the previous trading day's OCC prices, beside
   the usual SPY/RKLB/SPCX snapshot. Use the six Friday expiries from the coming Friday, plus Dec 18:

   ```
   .runtime/moomoo-env/bin/python ops/fetch_moomoo_chain.py --underlyings QQQ IWM --moneyness 0.03 \
       --expiries 2026-10-30 2026-11-06 2026-11-13 2026-11-20 2026-11-27 2026-12-18 \
       --output data/calibration/chain-prevclose-etf-YYYYMMDD.json
   .runtime/moomoo-env/bin/python ops/fetch_moomoo_chain.py --underlyings ASTS IONQ HOOD PLTR \
       --moneyness 0.2 --expiries ... --output data/calibration/chain-prevclose-mid-YYYYMMDD.json
   ```

   - QQQ and IWM close at 16:15 like SPY: they show whether SPY's whole-day offset is shared.
   - The mid-caps quote wide like RKLB, where single-stock errors are largest.
2. **After 20:10 ET** (after the scored close), fetch the previous day's 16:00 books of those codes:

   ```
   .venv/bin/python ops/fetch_ibkr_closing_books.py --chains data/calibration/chain-prevclose-etf-YYYYMMDD.json \
       data/calibration/chain-prevclose-mid-YYYYMMDD.json --chain-days PREVIOUS-DAY --moneyness 0.5 \
       --statements "" --client-offset 304 --concurrency 4 --timeout 90 --output data/calibration/ibkr-books-wide.json
   ```

   That is about 1,600–1,800 books a day, roughly 3 hours.
3. **Backtest:** pass each snapshot as `--prev-close FILE=PREVIOUS-DAY`, and the books file as one
   more `--chains` file.
