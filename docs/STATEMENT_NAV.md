# The scored close: IBKR Activity Statement NAV

The instructor scores each day on the IBKR Activity Statement's **Net Asset Value, Ending
Value** (confirmed Oct 23). That replaces the Oct 21 working assumption that the API closing
NetLiquidation was official, and the Oct 22 one that the NAV after the 20:00 ET post-market
close was. Implementation: `hedge/statement.py`.

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
  "in all cases". Our SPY legs fit 16:15, when SPY options stop trading, better.
- **Rounding:** the statement keeps the 4-decimal close (9.0497, 18.4774). Rounding the closes to
  cents does not reproduce the Oct 22 total; the unrounded closes do.

## What the statement NAV is

The Oct 22 statement reconciles to the cent:

| Component | Statement | Where it comes from |
|---|---|---|
| Cash | 872,744.50 | End-of-day cash; post-market trades (to 19:47) included. Equal to the API TotalCashValue after 20:00 |
| Interest accruals | 1,601.06 | API AccruedCash |
| Stock | 74,591.65 | RKLB 1,120 x **51.22** + SPCX 119 x **144.75**: the **regular-session (16:00) close**, IBKR's RTH daily TRADES bar. The after-hours marks gave 74,302.55 |
| Options | 34,437.00 | Each leg at its **closing NBBO midpoint**: 16:00 ET for SPCX/RKLB options, **16:15** for SPY options (they trade until 16:15) |
| **Total** | **983,374.21** | |

Option marks against our captured books:
- RKLB 45P 1.93 and SPCX 160C 3.70: the 16:00 midpoint, exact.
- SPCX 145C 9.0497: the 16:00 midpoint, 9.05.
- SPY legs (1120C 10.74, 1120P 14.415, 1127C 12.115, 1127P 15.24): between the 16:00 and 16:20
  midpoints, consistent with 16:15.
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
  - **Option marks:** the midpoint of the close-mark capture at the option's close. That is
    `pre_close` (last capture before 16:00) or `options_close` (last capture before 16:15, for SPY,
    QQQ, IWM and DIA options).
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
  - **Gap:** predicted statement NAV minus target. Its distribution covers only the option-mark
    uncertainty; spending goes toward its `spend_quantile`.
  - **No just-in-time wait:** the gap no longer moves with the market.
  - **Cost per round trip:** exactly the spread paid plus commissions.
- **Official close:** `api_close_official` is False. An API row is provisional and warns until the
  statement is imported.

## Each morning

Enter the previous day's statement Ending Value in Settings & alerts > Import an official
closing NAV. The Previous official close row warns until you do. Trading still starts at 04:00 on
the predicted value.
