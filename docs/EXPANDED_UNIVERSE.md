# Cross-instrument paper hedging

The practice allowlist now contains BA shares and options plus GE and UAL stock,
and SPY, XLI, ITA, QQQ, TLT, GLD and XLE ETF shares. US stocks and ETFs use qualified IBKR STK
contracts. All discretionary positions are jointly evaluated against the same
interest-adjusted closing NAV objective. Existing holdings remain in valuation
and risk calculations. Exact mandatory contracts cannot be traded away.

The universe is configurable; it is not restricted to BA. Read-only discovery:

```
python -m hedge.cli expand-universe --config CONFIG.yaml \
  --symbols SPY XLI ITA QQQ TLT GLD UAL GE XLE --output EXPANDED.yaml
python ops/fetch_moomoo_daily.py --symbols BA SPY XLI ITA QQQ TLT GLD UAL GE XLE \
  --as-of YYYY-MM-DD --output data/moomoo/daily-related-stocks.csv
```

Run the daily downloader in the isolated moomoo SDK environment. It creates only
OpenQuoteContext, downloads prior completed adjusted daily bars, and never
opens a trade context. Set `daily_stock_history` in the expanded manifest to the
CSV path; generate collector codes from the manifest's qualified contracts.
Pause and reconcile orders before deploying a manifest change. The normal
startup is paused; use shadow mode to inspect proposals before enabling paper.

Discovery accepts up to 12 underlying symbols per optimization universe to
bound data subscriptions and mixed-integer solve work. New symbols must have
unambiguous USD stock identities, live permitted books, and joint historical
coverage before activation. Stocks cannot be newly shorted without verified
borrow availability. Existing short positions remain represented and may be
reduced. Futures, FX, bonds and crypto are not activated by a ticker list;
they require validated pricing, settlement, margin and execution adapters.

Daily-return scenarios use the configured 20/60/120 prior-session window,
aligned across all represented symbols, with at least 20 observations, 20%
diagonal covariance shrinkage, and deterministic antithetic shocks. The model
has zero directional drift. Today's incomplete bar and future bars are excluded.
Daily volatility is scaled to the remaining calendar horizon; option IV/skew
paths remain modeled. Each symbol's IV shock is correlated with its own
standardized spot shock, `iv = sigma_iv * (rho * z_spot + sqrt(1 - rho^2) * u)`
with `u` an independent antithetic normal block: rho defaults to -0.7 for broad
index ETFs (SPY, IVV, VOO, SPLG, QQQ, IWM, DIA) and -0.4 for other symbols, and
`spot_vol_correlation: {SYMBOL: rho}` in the manifest overrides it (each in
[-0.95, 0.5]). The rho values used are recorded on the scenario set and in its
source text; next-close scenarios reuse the same joint shocks. Matched-clock
intraday paths keep their observed IV changes. Missing, duplicate or stale joint
history blocks scenario construction instead of substituting invented correlations. Sufficient matched
intraday option history takes precedence when supplied.

These are forecast assumptions, not demonstrated contest performance. A broader
universe cannot guarantee reaching the closing target or repairing earlier NAV
changes. No-trade remains a valid outcome if candidates do not improve expected
score enough after costs and uncertainty. The current practice improvement floor is $25, with an unchanged uncertainty
allowance, 20% local residual-delta cap and 3% stress-loss cap. See
FAST_PAPER_POLICY.md for the operator-selected faster evaluation schedule.

Tests cover read-only discovery, mandatory protection, multi-underlying scenario
coverage, deterministic covariance/shocks, future-row exclusion, missing-history
rejection, and unavailable borrow restrictions. Repeated stress repricing is
cached within each scenario build to avoid redundant option valuations.

## Related-security deployment, October 7

The operator authorized the first recommended additions: UAL, GE and XLE. DAL,
JETS, XAR and CVX remain comparison alternatives in RELATED_SECURITIES_RESEARCH.md;
SH remains unapproved. The local deployment now has 24 contracts across ten
underlying symbols, within the existing cap of 12.

Read-only IBKR qualification verified UAL conId 79498203, GE conId 498843743 and
XLE conId 4215217 as SMART-routed USD stocks. Reconciliation confirmed exact
mandatory quantities, zero working orders and zero unresolved intents before
restart. Scoring identity, closing NAV records, risk limits and schedules are
preserved. Moomoo remains quote-only; stock quote rights reported LV3.

The collector subscribes to the three additional stocks and records their minute
bars and quotes through the existing interfaces. After-hours crossed books are
rejected and observations remain frozen/nonexecutable; subscription access does
not establish fresh executable prices. All three names are visible in the restarted
dashboard. Ordinary operation and recovery remain SHADOW after market close.
No new securities were purchased to install the universe.

Aligned history for all ten symbols is in `data/moomoo/daily-related-stocks.csv`:
1,660 prior completed daily prices through October 6, provider QFQ adjustment,
with provenance in the adjacent JSON. The configured 60-session covariance
determines joint scenarios. XLE is not hardcoded as an inverse BA asset. Ordinary
hedge scenarios retain zero directional drift; new names do not inherit SPY/QQQ
recovery forecasts or paper-recovery approval.

A synthetic computation check compared seven versus ten symbols using representative
observed price levels, synthetic observation timestamps and illustrative new-stock
spreads/sizes. Scenario generation took 0.19/0.18 seconds; stock-only solves took
0.46/0.68 seconds; full solves took 1.56/1.41 seconds. All returned OPTIMAL,
preserved mandates and passed numerical combined-portfolio limits. This is one
latency fixture, not live execution evidence, a deadline guarantee or held-out
contest performance. The 10-second deadline is unchanged.

The focused universe/valuation/optimizer/risk/market-data suite passed 37 tests.
The new related-stock regression checks positive UAL/GE and negative XLE fixture
covariances, scenario coverage, unavailable short borrow, exact mandates and
independent combined risk validation.

Paper activation still uses normal preflight: broker reconciliation, fresh
regular-session quotes, previous closing NAV, cash benchmark assumptions and
execution-path checks. No stale quote or missing baseline is bypassed.
Rollback copies are local `config/local.practice.before-related.yaml` and
`config/local.moomoo-codes.before-related.json`; restore both together after
pausing/reconciling and stopping the service. Retain every filled holding in any
rollback configuration, then restart paused and verify before activation.
