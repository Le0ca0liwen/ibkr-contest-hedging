> **Latest clarification:** The organizer uses the actual daily cash rate, not fixed 3.35%. Current practice mode is `actual_daily_cash_rate`; target = previous official ending NAV × (1 + sourced daily fractional rate). Missing rates leave targets/scores unknown. Accrued interest is not subtracted separately. Earlier fixed-rate examples below are superseded. Record dated rates under Settings & alerts.

# Contest Hedge

> **Confirmed scoring revision:** Score = sum |V_today − V_previous × (1 + r/360)|. The instructor supplied r ≈ 3.35%; the practice manifest uses 0.0335. Raw IBKR NAV includes accrued interest. Apply the benchmark once; do not subtract actual interest again. Official closes come from IBKR Custom Statement → Net Asset Value. Day 0 is October 6; scored sessions are October 7–9. Paper execution still requires fresh validated data, mandatory holdings, margin/risk checks, and the official baseline.


A local, paper-only options hedging system and browser dashboard for the IBKR contest. The objective is **the sum of absolute differences between consecutive official closing account NAVs**. Gains and losses count equally. Day 0 establishes the baseline and is not scored separately.

The code includes American-option repricing, integer scenario optimization, an independent risk gate, durable order intents, reconciliation, a quote-based replay engine, and an optional advisory AI supervisor. **No actual IBKR connection, contest activation, or empirical performance claim is implied by running the demo.**

## Practice setup

The [Google AI supervisor](docs/GOOGLE_SUPERVISOR.md) defaults to Gemini 3.8
Flash and supports model selection in Hedge decisions. It provides bounded
advice only and runs independently of trading; setup never enables orders.

Moomoo is being evaluated strictly as a **market-data-only** source. The isolated
quote diagnostic, OpenD setup, entitlements, and validation gates are described
in [Moomoo market data](docs/MOOMOO_MARKET_DATA.md). It does not enable execution;
IBKR remains the sole trading adapter.

Follow [the practice runbook](docs/PRACTICE_RUNBOOK.md) for paper TWS setup, read-only account/contract discovery, empty-account purchase rehearsal, and Day 0 operation. Run `.venv/bin/python -m hedge.cli serve --rehearsal` (or double-click `Practice Rehearsal.command`) to test buying the mandatory options before hedging. Round dates live in the local manifest. The current practice schedules mandatory purchases on Day 0; scoring starts on the next configured session. Purchase timing is configured separately and must be revised explicitly if Day 0 is missed.

## Open the dashboard

The independent [surplus research add-on](docs/SURPLUS_ADDON.md) runs on
<http://127.0.0.1:8770/>. Double-click `Start Surplus Add-on.command` or run
`.venv/bin/python -m surplus_addon serve`. It reads the main app, shows bounded
expense-cycle proposals, and supports a fictional feedback rehearsal. It cannot
submit IBKR or moomoo orders; paper expense execution is not integrated.

Dependencies are already installed in this workspace's `.venv` (Python 3.12).

```sh
.venv/bin/python -m hedge.cli serve --demo
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The prominent **SYNTHETIC DEMO** banner identifies simulated prices, NAV, dates, positions, and executions. Click **Start shadow** to see proposed hedges without fills; **Enable simulated trading** exercises the full local execution path. **Pause** requests cancellations and retains filled positions.

The dashboard provides:

- Overview: NAV, previous official close, provisional score, official score ledger, risk envelope, and latest decision.
- Positions & risk: actual mandatory/hedge holdings, delta/gamma/vega/theta, and failed checks.
- Trades: order creation/submission, broker acknowledgments, partial/final executions, fees, cancellations, and decision links. Times use Eastern time with the timezone shown.
- Decisions: expected closing score, improvement after costs, eligibility, immutable snapshot/scenario/configuration identifiers, and AI explanations when enabled.
- Settings & alerts: validated profiles, effective hard limits, Day 0 setup, official close import, and actionable alerts.
- CSV export: scores, orders, executions, and decisions.

UI controls are localhost-only and protected by an unpredictable control token and same-origin checks. The browser receives no broker credentials. GitHub Actions is for tests, not unattended trading or runtime updates.

## Fresh installation

Install Python **3.12**, then:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m pytest -q
```

`requirements.lock` pins runtime and test dependencies. The macOS virtual environment itself is not portable; recreate it on another machine. Linux CI uses the same pinned Python packages.

## Connect your student paper account

Enabled paper operation uses [recoverable data waiting](docs/TRADING_CONTINUITY.md)
for temporary quote/connection interruptions. It cancels system orders where
possible, reconciles, and repeats the full preflight before automatically resuming.
Manual pauses and safety stops still disable execution.

1. Install IB Gateway (or TWS), authenticate to the instructor's **paper** account, and allow local socket API access. Gateway paper default: `4002`; TWS paper default: `7497`. The configured accessible `DU…` account is checked independently of the port.
2. Start with the Gateway/TWS **Read-Only API** setting enabled. Verify API-accessible live stock and option subscriptions. Delayed or frozen prices cannot authorize execution. Student account entitlements may require the instructor's help.
3. Copy `config/practice.template.yaml` to `config/local.contest.yaml`. Obtain the corrected dates, expiry, contract IDs, multiplier, scoring/report convention, and exact mandatory quantities. **The template is intentionally invalid. No conflicting practice dates are guessed.**
4. Populate `contracts` with verified USD stocks and equity options; `mandatory` maps exact contract IDs to quantities; `allowlist` contains other instruments only. Every option must identify its underlying stock contract. All option expiries must cover the contest holding period.
5. Validate and run shadow mode first:

```sh
.venv/bin/python -m hedge.cli validate config/local.contest.yaml
.venv/bin/python -m hedge.cli serve --config config/local.contest.yaml
```

The service starts **paused**, tries a read-only connection, qualifies every contract against broker metadata, and presents problems on the dashboard. **Start shadow** activates calculations; it never submits orders.

6. Prepare PostgreSQL, set `HEDGE_DATABASE_URL`, and migrate. SQLite is allowed for demo/research/shadow; **IBKR automatic execution requires PostgreSQL**.

```sh
docker compose up -d postgres
export HEDGE_DATABASE_URL='postgresql+psycopg://hedge:hedge@localhost:5432/hedge'
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m hedge.cli serve --config config/local.contest.yaml
```

The Compose password is a local development default. Configure your own database password for the operational account; keep it outside Git. Docker is optional: any local PostgreSQL server with this connection URL works.

7. Set `confirmed: true` only after resolving the official manifest. Enable Gateway's order-capable API setting after shadow validation. The dashboard's paper activation checks account identity, qualification, reconciliation, quotes, mandates, risk, and unresolved orders. Import the previous official close before a scored session.

**Mandatory setup** buys only missing required quantities on the configured purchase session, never exceeds the mandate, and independently checks each slice. The usual purchase session is Day 0; this practice explicitly uses Day 1. Arm opening purchases in Settings & alerts or start them manually during the purchase window. Scheduled hedging waits for reconciliation and the previous official NAV. Day 1 costs are scored. Mandatory options are never discretionary hedge instruments.

Keep the Mac awake, online, and connected. Gateway restart/manual reauthentication remain operator responsibilities. The launchd template in `ops/` is optional and must be filled with your absolute project/configuration/database paths. It is not installed automatically.

## Manifest contract example (format only)

```yaml
contracts:
  - con_id: VERIFIED_STOCK_ID
    symbol: BA
    kind: STK
  - con_id: VERIFIED_CALL_ID
    symbol: BA
    kind: OPT
    multiplier: 100
    expiry: CONFIRMED_ISO_DATE
    strike: 195
    right: C
    underlying_id: VERIFIED_STOCK_ID
mandatory:
  VERIFIED_CALL_ID: 25
allowlist:
  - VERIFIED_STOCK_ID
```

Replace every placeholder. Include the put and all approved hedge contracts. Default limits cannot be enlarged beyond the agreed maximums by the manifest. Futures/non-USD instruments require additional validated adapters; they cannot be activated merely by changing a security-type string.

## Official scoring

Enter reported NAV in Settings or import an account-scoped CSV:

```text
date,nav,source,account
2026-10-06,1000000.00,Official IBKR Day 0 report,DU_YOUR_ACCOUNT
2026-10-07,1005000.00,Official IBKR Day 1 report,DU_YOUR_ACCOUNT
2026-10-08,1000000.00,Official IBKR Day 2 report,DU_YOUR_ACCOUNT
```

These dates illustrate formatting, not confirmed contest dates. That series scores **$10,000**. Fees already included in report NAV are not deducted again. Revisions preserve original rows and recompute adjacent scores. Missing closes are never bridged. Strategy/allowlist configuration revisions do not erase the same contest's official ledger.

```sh
.venv/bin/python -m hedge.cli import-closes --config config/local.contest.yaml reports/closes.csv
```

Live NAV and close snapshots are **provisional**, not an automatic substitute for the organizer's valuation. Synthetic replay uses midpoint NAV as an explicitly labeled proxy. Confirm the report and mark convention with the instructor.

## Research and data

Live quotes are archived to ignored `data/quotes/*.parquet`. IBKR expired-option/EOD-option history is not available; no paid archive purchase is made by this application. Cboe DataShop import preserves NBBO, sizes, underlying BBO, optional IV, and provenance:

```sh
.venv/bin/python -m hedge.cli import-cboe reports/cboe.csv data/cboe.parquet
.venv/bin/python -m hedge.cli fetch-history --config config/local.contest.yaml --con-id VERIFIED_ID --output data/bars.parquet
.venv/bin/python -m hedge.cli backtest --config config/local.historical.yaml --quotes data/quotes.parquet --strategy dynamic_l1
.venv/bin/python -m hedge.cli walk-forward --config config/local.contest.yaml --quotes data/cboe.parquet --reference-spot BA=195
```

Single-window replay requires a historical manifest with actual archival contract IDs and matching dates. Walk-forward constructs historically available Day 0 mandates, matching the original portfolio's time to expiry and moneyness using **explicit original reference spots**. The IDs generated for archive contracts are research identifiers, never broker IDs.

Available strategies: dynamic L1, delta neutralization, delta/gamma/vega neutralization, minimum variance with integer risk projection, historical L1, and two-close lookahead. Policies include threshold, periodic, daily, static, and unhedged. Walk-forward sweeps 1/5/15/30/60/daily evaluation, 20/60/120-session windows, and $25/$100/$250 thresholds. Selection uses preceding validation data, not test outcomes. Large sweeps can take substantial time; run small experiments first.

Forecasts include calendar time and volatility shocks. When historical IV data is missing, the system labels the scenarios as **modeled assumptions**; it does not represent them as empirical evidence. Broker-provided option Greek units are normalized. American valuation includes configured dividend inputs; dividends and borrow/financing assumptions must be maintained as known at decision time.

Replay fills occur at subsequent observed executable quotes with conservative sizes, spread/slippage, fees, financing, and borrow. Missing closing observations invalidate a window. Archived midpoint scores and the paper simulator are not proofs of live or contest performance. Independent assignment/expiry fault cases are exercised by reconciliation/risk tests; historical automatic exercise/assignment decisions require additional broker-specific historical evidence.

Stock replay execution requires explicit underlying BBO sizes and short-availability fields; absent values block those fills rather than inventing liquidity/borrow. Supplement DataShop's underlying prices with an appropriate stock quote/borrow archive to compare stock-short strategies fairly.

## Optional AI supervisor

The trading service continuously monitors risk without an AI dependency. AI runs asynchronously, at most once per 15 minutes. It receives compact snapshots without account identifiers or credentials and produces a validated explanation/profile recommendation. It has no numerical trade, limit-editing, broker, or execution tools.

Set `OPENAI_API_KEY` and `HEDGE_AI_MODEL` in the local process environment to enable API reviews; leave them unset to avoid any API calls. Reviews are **advisory-only**. Only `STANDARD` is initially validated for IBKR operation. Alternative profiles are available in the synthetic demo and research; they need contest validation before operational promotion. AI failure leaves the deterministic strategy active.

## Recovery and operation

- **Pause:** stops new orders, requests system-owned cancellations, and retains filled positions. The UI distinguishes pending cancellation from confirmed pause. Late fills are recorded.
- **UNKNOWN submission:** blocks subsequent orders. Reconcile open orders, completed orders, and executions; consult IBKR reports if the outage exceeded API history. Absence from an open-order snapshot is never permission to resend.
- **Restart:** durable intent precedes submission. Unsent intents are rejected locally; uncertain submissions are reconciled, not retried. The service always restarts paused.
- **Manual trade/assignment:** actual broker positions update the portfolio; a mismatch or unsupported instrument blocks ordinary trading. Mandatory contract mismatches need operator/instructor resolution.
- **Close/end:** retain useful filled hedges overnight; stop ordinary option orders 15 minutes and stock orders 2 minutes before exchange close. Final contest close stops submissions and requests cancellations.
- **Second computer:** optional read-only snapshot/replay checking only; no distributed order writer or automatic failover is deployed.

There is one execution writer per account (local lock plus PostgreSQL advisory lock). The dashboard is local-only. Do not expose the broker socket or dashboard to the public internet.

## Tests and remaining external acceptance gates

See [Market factors and timely execution](docs/EXECUTION_FACTORS.md) for SPY beta
data requirements, modeled rho and rate stresses, Greek units, and bounded
limit/market routing. Missing beta history is displayed as unavailable.

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check hedge tests
.venv/bin/python -m alembic upgrade head
```

Tests cover scoring, corrected reports, mandates, calendars/early closes, American pricing and multipliers, scenario/solver determinism, nonlinear risk, stale data, execution deduplication, partial fills, cancel/fill races, crashes around transmission, account rejection, CSRF, and AI boundaries. PostgreSQL integration runs when `TEST_POSTGRES_URL` is set (configured in CI).

**Before contest activation:** provide corrected official dates/expiry/IDs; verify student account permissions and data; reconcile the organizer's closing NAV convention; validate real Gateway callbacks and PostgreSQL durability; run shadow and practice sessions; select a strategy using adequate historical/forward evidence. The software's tests and synthetic demonstration do not replace these account-specific acceptance gates.
