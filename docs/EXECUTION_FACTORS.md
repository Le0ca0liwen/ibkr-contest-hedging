# Market factors and timely execution

The objective remains the configured contest score: reduce expected absolute
closing NAV deviations after modeled costs and uncertainty. More frequent orders
and neutralizing every Greek independently are not assumed to improve this score.

## Factor coverage

- Delta, gamma, theta and vega remain included through full American-option
  repricing under spot, volatility, skew and calendar-time scenarios.
- Rho is a central difference using a one-basis-point rate bump. Internal rho is
  per share per unit rate change; the risk dashboard reports position dollars per
  **one percentage-point** rate change, including contract multipliers. It is a
  model estimate, not an IBKR-supplied Greek. Stock rho is zero in this valuation
  model; financing and cash carry remain separate.
- The hard stress constraints additionally include parallel rate moves of ±100
  basis points by default (`risk.rate_stress_shift: 0.01`), using full option
  repricing. This is a stress assumption, not an interest-rate forecast.
- Beta uses ordinary least squares against `beta_benchmark: SPY`, from aligned
  completed-session returns in `daily_stock_history`. The CSV must contain
  `date,symbol,close` for SPY and every underlying in the selected universe. Supply
  consistently adjusted closes; at least 21 aligned closing prices are required,
  with the latest no more than seven calendar days old. The history window uses
  `policy.history_sessions`. Current/future dates, duplicate observations,
  nonfinite prices and zero benchmark variance cannot supply a beta estimate.
- With complete beta coverage, additional ±5% benchmark shocks are mapped to
  each underlying using its estimated beta and fully repriced. Existing global
  and individual-underlying stresses remain in place. Portfolio beta is the sum
  of beta-weighted dollar delta divided by the risk budget; it is a local
  sensitivity, not a guarantee about nonlinear option returns.
- Missing benchmark data is explicitly unavailable, not beta=1 or zero risk.
  Existing spot/IV/time/rate scenarios still run. Factor coverage and the beta
  estimation source appear under Positions & risk. Scenario IDs include beta
  and rho inputs for reproducibility.

The optional `greek` comparison strategy uses dollar sensitivities to 1% spot,
one-point IV/rate moves and one day of decay, plus benchmark delta when available.
The default `dynamic_l1` strategy continues to optimize the contest score under
the expanded stress constraints; the strategy is not switched automatically.

## Limit and market routing

Ordinary slices start at a tick-valid midpoint limit and progress toward the
executable quote after the configured wait. Existing orders are managed before
running a new optimization. Corrective orders and orders within 90 seconds of
their configured cutoff bypass the ordinary reprice wait and use an executable
limit, subject to the original price budget and fresh risk/economic checks.

Urgent buys round the actual ask up to a valid tick; urgent sells round the actual
bid down. If that tick would exceed the modeled budget, submission is rejected
or the existing order is cancelled. A floating midpoint calculation is not used
to reconstruct these executable prices. Paused/read-only execution cancels
working orders instead of repricing them.

Market orders remain restricted to newly submitted corrective stock slices with
live quotes at most one second old, spreads at most $0.02, and both book sizes at
least five times the slice size by default. The slippage reserve is included in
risk checks, but is not a guaranteed fill-price cap. Options and protected setup
packages remain limit orders. Existing limit orders are not blindly converted
to market orders; cancellation and reconciliation still precede a new slice.

These rules improve execution responsiveness and factor coverage. They do not
establish optimal real fill quality or better contest performance. Evaluate
realistic quote replay and IBKR paper fills before changing account settings.

## Consistent execution scenarios

Each scenario set records its valuation time and the spot/IV inputs for every
included contract. Immediately before a single-order or protected-package
submission, and before managing an acknowledged order, execution checks these
inputs and a maximum age of `policy.risk_seconds`. Changed or older ordinary
scenarios are rebuilt off the event loop using the service's existing history
configuration and the same instrument IDs. Inputs are checked again after the
build. A second market move rejects submission or cancels the working order so
the service can reconcile and replan; it does not loop indefinitely trying to
catch a moving quote. Pause, holdings, working orders, spread, cost and partial
fill path checks still apply after rebuilding. Pretransmission records retain
the actual scenario ID and timestamp used for the final check.

Recovery scenarios contain additional forecast adjustments. A stale recovery
scenario requires a new recovery plan; replacing it with an ordinary scenario
would discard that forecast. An unknown scenario provenance likewise triggers
refresh rather than being treated as current. Fresh market data checks remain
independent: unchanged spot/IV does not make an old quote eligible to trade.

## Execution quality

The Trades page and `/api/export/execution_quality` expose one row per durable
order intent (one per leg for a protected package), reconstructed from persisted
records rather than in-memory counters:

- Filled/requested quantity and fill ratio use effective execution reports;
  submission or a Filled status callback alone does not invent a fill. A ratio
  is unavailable until a durable submission attempt exists.
- First, last and quantity-weighted fill waits start at the durable SUBMITTING
  transition. They include acknowledgement/network time and are not exchange
  queue-latency estimates. Invalid negative intervals are unavailable.
- Decision slippage uses the midpoint captured with the decision inputs; arrival
  slippage uses the initial slice/package-leg reference midpoint. Positive dollar
  slippage is adverse for both buys and sells and includes the contract multiplier.
  Partial fills measure only executed quantities, not opportunity cost of unfilled
  orders. Old decisions lacking a stored midpoint remain unavailable.
- Known USD commissions are shown separately. Total decision cost is only given
  when the decision reference and all USD commissions for effective fills are
  available. Non-USD and missing fees are not silently treated as zero. Later fee
  reports and execution corrections update the calculation without counting the
  replaced execution twice. Net combo commission reports are not charged again
  on top of individual leg fees.
- Amendments and cancellations count recorded local attempts, not broker-confirmed
  successes. Cancel attempts are linked to intent IDs to avoid reused broker-order
  IDs mixing histories. Package attempts appear on both leg rows; do not sum those
  shared counts across legs. Unlinked/manual executions are not attributed to a
  strategy intent.

The report covers the account's stored intents and survives process restarts.
Each calculation uses three database queries: grouped audit records, effective
fills with account-scoped commissions, and raw intents. It reuses the loaded
plans and does not cache metrics, so late commissions and corrections remain
visible on the next refresh. This reduces query overhead without changing the
pretransmission risk checks. The report still reads the account's full history;
large histories may need a separate paginated reporting workflow.
Synthetic fills remain synthetic evidence; this report does not establish live
fill quality or demonstrate that a routing policy improves contest scores.
