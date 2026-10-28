# SPCX / RKLB / SPY contest restrictions

Use `config/contest.template.yaml` for this contest, not the BA practice
template. Set `contest_rules: spcx_rklb_spy` and fill in the verified
`contest_option_expiry`, contract definitions, account, calendar, and source.
The updated table confirms November 20 and the strikes below. The year and
real broker conIds still require confirmation; neither is inferred. The template intentionally cannot activate as supplied.

The manifest validates exactly seven mandatory legs, all standard 100-share
options at the confirmed expiry:

| Position | Required quantities |
| --- | --- |
| SPCX calls, strikes 130 / 145 / 160 | +25 / -50 / +25 |
| RKLB puts, strikes 45 / 55 | -25 / +25 |
| SPY call and put, strike 770 | +25 / +25 |

The following are forbidden for both long and short holdings:

- SPY shares, at any time.
- RKLB calls at the confirmed contest expiry.
- SPY options at that expiry, except the two mandatory contracts.

With `universe: open` (the contest template default) there is no hand-written
allowlist: every defined contract except the mandated legs and the forbidden
instruments above is tradable, and the allowlist is derived on every load.
Contracts still have to be defined (qualified conId, live quotes, history) for
the optimizer to price them; `hedge.cli expand-universe --symbols ...
--option-symbols SPCX RKLB SPY --expiries N --strikes K` qualifies stocks and
nearby option chains read-only and never adds a forbidden contract. Existing
caps (12 underlyings, 6 option names per underlying, 50 contracts per option,
risk limits) are unchanged.

SPY stock can remain in `contracts` for underlying market data. It cannot be in
`allowlist`. Other-expiry SPY options, other-expiry RKLB calls, SPCX/RKLB shares,
and other permitted stock/option hedges still need explicit approval through the
allowlist and must pass the ordinary risk checks. This version only supports
stocks and options; permission in the contest rules does not add futures support.

Validation rejects forbidden allowlist entries. Screening and optimization also
filter them independently; the final risk gate rejects forbidden target
positions, actual holdings, and working orders, including in corrective mode.
Held forbidden instruments remain visible to risk calculations. If such a
holding exists, automatic trading stops for reconciliation; the system does not
silently submit a liquidation outside its approved universe.

Outside initial setup, both actual and proposed mandatory quantities must match
exactly, including the negative legs. The legacy automatic setup adapter supports
long mandates paired with protective options, not this seven-leg signed
portfolio. It is explicitly disabled for this profile. Establishing the mandated
portfolio requires a separate validated setup workflow; once holdings reconcile
and all other readiness checks pass, ordinary hedge trading can use this profile.
Short mandatory legs are included in assignment-exposure reporting and
conservative incremental-margin estimates.

## Contest calendar and scoring

Day 0 is Wednesday 2026-10-21 (setup and baseline). Days 1-10 are Oct 22, 23,
26-30 and Nov 2-4; all mandated options expire Friday 2026-11-20 (the handout's
"November 21, 2025" is a stale year).

Score = sum over days 1-10 of |V_i − (1 + r/360)^n_i × V_(i−1)|, r = 3.44%.
`scoring_rule: calendar_interest_benchmark` implements this: n_i is the number of
calendar days since the previous contest session, so it is 3 on Mondays
(Oct 26, Nov 2) and 1 otherwise. The template sets these dates, the expiry and
the rate, and `policy.cash_rate: 0.0344` so NAV forecasts include cash interest.
The account, report source and verified conIds must still be filled in.

Gains and losses score equally, so a NAV surplus over the target costs points.
The instructor does not disallow spending it deliberately through ordinary trades
against the market. The template therefore sets `policy.burn_surplus: false`
(no all-day burning through hedge costs) and offers an optional closing surplus
burner that acts only in the last minutes of a scored session, in allowlisted
SPCX/RKLB shares (never SPY shares, never the mandated legs). See
[CLOSING_BURNER.md](CLOSING_BURNER.md).

## Contest setup checklist (Moomoo data, IBKR broker)

IBKR provides no live quotes to this team; Moomoo OpenD is the only market-data
source and never trades. Before Day 0:

1. With TWS/Gateway on the paper account (Read-Only API is enough), qualify the
   three stocks and seven legs from the final strikes (the handout says strikes
   are set shortly before trading):
   `python -m hedge.cli prepare-contest --spcx 130 145 160 --rklb 45 55 --spy 770 --port 7497`
   (7497 is TWS paper; 4002 is IB Gateway). It writes `config/local.contest.yaml` with
   `market_data_provider: moomoo` and `confirmed: false`; set `initial_nav` to the
   verified Day 0 NAV (`python -m hedge.cli probe-ibkr` shows it).
2. Fetch joint daily history (SPY is required for beta even though its shares are
   forbidden), and re-run it before **every** open; the service reloads the file
   and waits rather than trading if it is over 7 days old:
   `.runtime/moomoo-env/bin/python ops/fetch_moomoo_daily.py --symbols SPCX RKLB SPY IVV VOO --as-of <today> --output data/moomoo/daily-contest.csv`.
   SPCX needs at least 21 completed sessions.
3. Discover hedge candidates read-only, then regenerate the collector list:
   `python -m hedge.cli expand-universe --config config/local.contest.yaml --option-symbols SPCX RKLB SPY --expiries 2 --strikes 1 --output config/local.contest.yaml`
   This defines about 10 options (SPY Nov 19/23 770C/P; SPCX Nov 20 145P and Nov 27 145C/P;
   RKLB Nov 20 50P and Nov 27 50C/P) plus SPCX/RKLB stock: about 12 tradable candidates.
   Measured with realistic quotes, full solves certify in 1-3.5 s and scenario builds take
   ~0.7 s; with ~45 candidates solves often timed out (no hedge) and builds exceeded the
   transmission freshness window. Add `--symbols IVV` only with verified `stock_borrow`.
   then `python -m hedge.cli moomoo-codes --config config/local.contest.yaml`.
   Discovery and screening use the expiries nearest Nov 20 on both sides (for
   SPY, Nov 19/23 because every other Nov 20 SPY option is forbidden).
4. Optional: record verified shortable shares in `stock_borrow` with
   `stock_borrow_source`; Moomoo has no borrow data, so new stock shorts are
   otherwise disabled (options can still hedge).
5. `python -m hedge.cli validate config/local.contest.yaml`. Set `confirmed: true`
   once the seven legs, account and dates are checked; paper trading refuses an
   unconfirmed configuration.

## Launching the contest (dashboard only)

Double-click **Start Contest.command**. It validates `config/local.contest.yaml`,
starts the project-local PostgreSQL database `hedge_contest` (automatic paper
trading refuses SQLite), rewrites the Moomoo collector list from the
configuration, refreshes the daily price history from Moomoo (quote-only), keeps
the Mac awake while the service runs, and opens http://127.0.0.1:8765. Control
the rest from the dashboard.

- **Automatic paper start** (`autostart: paper`, the template default; the
  dashboard "Auto paper" switch overrides it and is remembered). After launch the
  service waits until the seven mandated legs are held, then hands over to the
  same checks as "Enable paper trading": confirmed configuration, IBKR contract
  identities, PostgreSQL, a clean reconcile, live quotes, the previous official
  close, the risk limits and the order window. Before the open it waits for the
  trading window; if a check fails it retries every 15 s and the health line
  names the cause. Any operator control (Pause, Start shadow, Enable paper
  trading) cancels a pending automatic start; "Auto paper: ON" turns it back on
  after a confirmation.
- The seven legs are bought by hand in TWS (automatic contest setup is not
  supported). Close any non-contest positions first: reconciliation refuses
  holdings that are not defined in the configuration.
- The TWS API must not be Read-Only for paper trading.
- The closing burner is configured for paper in the template; it trades only
  while the app is in paper, only from close-20 to close-4 min on scored days.
  The burner panel can step it down to shadow or off at any time.

**One S&P 500 exposure.** The residual-delta cap (`risk.residual_delta` x budget) applies per
exposure group, not per symbol.
- **The group:** SPY, IVV, VOO and SPLG track the same index, so their dollar deltas add. An IVV
  short offsets SPY options' delta, and an IVV long adds to it (operator decision, Oct 28).
- **Everything else** is its own group.
- **Where it shows:** each group's total is the `residual_delta:<group>` risk metric and the
  "Residual delta cap: S&P 500" reason. The per-symbol `delta:<symbol>` metrics remain for display.
- **Who applies it:** the optimizer, the risk gate and corrective mode all use the groups.
- **Example:** on Oct 28 at 12:55, the SPY options' $98.2k and the 20 IVV's $15.5k make $113.7k
  against a $98.4k cap.

**Gross-cap options (both off by default; set under `risk:`).** The 3x discretionary gross cap is
the app's own leverage limit, not a contest rule. It counts each contract at |quantity| x
multiplier x underlying price, so on Oct 28 the 37 short SPY Nov 27 options used $2.85M of the
$2.95M cap and left no room for an IVV hedge of the S&P exposure.
- **C, `gross_basis: delta`:** an option counts at |delta| x its notional; shares count the same.
- **D, `gross_risk_reducing: 3.5`:** above `gross_notional` and up to this multiple, a trade is
  allowed only if it lowers both the holdings' stress loss (without trading costs) and their
  market exposure (|beta-weighted delta|, else the residual deltas' total) against the current
  holdings. Holding or shrinking gross there is always allowed.
  - **In the optimizer:** when its target reaches 98% of the cap, or no target is feasible, it
    solves a second time with that ceiling and those two constraints, and keeps the lower expected
    score (`solver_metrics.risk_reducing`).
  - **In the risk gate:** it refuses any other trade into that band with "Gross above 3x NAV only
    for trades that lower stress loss and market exposure".
- `ops/replay_limits.py` re-solves a day's recorded decisions under each variant and values the
  trades at the close.

Risk notes: the seven fixed legs are reported as `mandated_gross_notional` and
are outside the discretionary 3x gross cap (the SPY straddle alone is ~$3.85M of
underlying). Held options that are not being traded may have an unchanged
(quiet) Moomoo book for up to `held_option_quote_age` (300 s) before hedging
waits; anything traded still needs a book under 60 s old. Each session's last
pre-close and one post-close capture of all quotes, Greeks, IBKR position marks
and risk metrics are stored as `close_marks` records for score attribution.
