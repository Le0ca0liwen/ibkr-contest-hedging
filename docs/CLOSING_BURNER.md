# Closing surplus burner

The contest scores each session as `|V_i - V_(i-1) (1 + r/360)^n_i|` on the official
closing NAV, so a surplus above the target costs exactly as much as a deficit. The
instructor does not disallow spending a surplus deliberately through ordinary trades
against the market. The closing burner does that, but **only near the close**: if it
burned earlier, a later market drop could leave NAV below the target with nothing left
to undo.

In the contest the scored close is the IBKR Activity Statement NAV (docs/STATEMENT_NAV.md):
every position at its closing mark (shares at the 16:00 close, options at their closing
midpoint, SPY options at 16:15) plus cash after every trade booked that day, post-market
trades up to 20:00 ET included. Once the last mark is fixed the market no longer moves it, so
in the contest the burner works **after** the close: from 16:16 ET (`marks_fixed_at`) until the
post-market entry cutoff (19:56 ET), shares only (options trade in regular hours only). There
its gap is the predicted statement NAV minus the target, uncertain only by the option marks'
closing spreads; each round trip costs exactly the spread paid plus commissions, and there is
no just-in-time wait. Elsewhere the window is the last `window_minutes` before the regular close.

The burner is off by default (`burner.mode: off`). It shares the single execution owner
(TradingService, IBKR paper only, Moomoo quotes only) and never trades the seven
mandated legs or SPY shares.

## What it decides

Every step (about once a second, and immediately after its own order events) the burner
computes the forecast closing gap per scenario, the same quantity the surplus add-on
uses:

```
z = nav_estimate - target + bias + scenarios.changes @ holdings + cash carry to the close
```

- `target` is today's closing target (`closing_target`: previous official close times
  `(1 + r/360)^n`). Day 0 is unscored and the burner never acts on it.
- `nav_estimate` already contains every earlier burn loss and fee, so nothing is
  subtracted twice.
- `bias` is a signed, calibrated model-versus-official correction. It needs
  `bias_source` when nonzero.
- **After the close (contest)** the scenario term is gone: `z = predicted statement NAV - target
  + bias`, with 64 equally likely values that draw each option mark uniformly within its
  closing bid/ask (`TradingService.statement_estimate`). `require_pnl_stream` applies only
  before the close. With `post_close_basis: worst_case` (the code default) `z` is the single value
  `low - target + bias`: every option at the least favorable side of its closing bid/ask, so a
  burn never takes the statement NAV below target whatever the option prices turn out to be.
  With `prediction` (the contest's setting from Oct 26) `z` is the single value
  `predicted statement NAV - target + bias - post_close_margin`, falling back to the worst case
  when any held option's mark is not the OCC rebuild (a midpoint fallback); `unrebuilt` in the
  decision names them. `quantile` spends toward `spend_quantile` of the 64 samples.

  Why `prediction` with a $50 margin (Oct 25 comparison, live rebuild settings on the real Oct
  21-23 closing books of the held legs): the rebuild missed the statement by +$30, +$26 and +$29
  (above it each day). The samples are drawn across each book's whole width, so their spread
  ($520-1,700) dwarfs that error, and their quantiles sit far from the prediction: the 0.4
  quantile was +$183, +$20 and -$301 from it, the worst case -$1,434 to -$4,858. Simulated with a
  surplus uniform on $0-2,000, the expected |statement NAV - target| after the burn is:

  | Rule | error sd $25 | $75 | $150 | $300 |
  |---|---|---|---|---|
  | worst case | 970 | 967 | 968 | 960 |
  | quantile 0.4 (Oct 26 plan) | 171 | 178 | 200 | 273 |
  | quantile 0.5 | 238 | 247 | 268 | 334 |
  | prediction | 32 | 64 | 120 | 231 |
  | prediction - $50 | 27 | 62 | 118 | 228 |
  | prediction - $100 | 70 | 83 | 127 | 231 |

  Near target (surplus -$300 to +$300) the $50 margin is again within a few dollars of the best
  rule up to an error sd of $150; at sd $300 the worst case (149) beats it (188). Three days of
  held-leg books is a small sample: a miss as large as Oct 21's RKLB 55P before the dense fetch
  (-$568) would pass straight through.

`needed = max(0, quantile(z, spend_quantile))` (lower weighted quantile of the 64 scenarios;
0.5 is the median). Spending `L` changes the expected score by `E|z| - E|z - L|`, which keeps
improving while `L < median(z)`. The L1-optimal spend is the median, and the burner never plans
past it. The contest uses `spend_quantile: 0.4`: near the median the expected-score curve is
flat, so spending a little less costs little, while a burn cannot be undone if the forecast was
high.

**What the Oct 22 overspend taught (redesign of Oct 23).** On Oct 22 the burner spent $292
against a surplus that was gone by the close. Two things drove it:

- Its NAV came from fill attribution, which re-anchors on every NetLiquidation push,
  including pushes sent seconds after a fill that are briefly off by up to ~$1,000.
- It re-decided every second on a noisy forecast. Spending cannot be undone, so it spent on
  every upward blip and never recovered the downward ones.

It now guards against both:

- **Settled NAV only** (`require_pnl_stream`). The burner acts only when the NAV estimate is
  IBKR's 1-second P&L stream rolled forward from a settled NetLiquidation push (no fill
  within 20 s of it). On fill attribution it waits.
- **Post-fill settle** (`settle_seconds`). After a cycle that filled, it waits this long, so
  the fills and commissions reach the stream before the next decision.
- **A spend budget that only ratchets down.** Each planned cycle sets
  `budget = min(budget, spent + needed)`, and a cycle may use at most
  `min(needed, budget - spent)`. A forecast that drifts up later never raises it; once
  `budget - spent <= stop_threshold` the burner stops for the session. Shadow lowers it at
  most once per assumed cycle. After a restart it resumes from the lowest budget recorded on
  the session's cycles.

The remaining rules:

- **Hysteresis.** The burner engages when `needed > start_threshold` (default $25) and
  stays engaged until `needed <= stop_threshold` (default $5).
- **Cycle cap.** A cycle may plan at most `needed - stop_threshold / 2`.
- **Largest eligible candidate.** It picks the largest round trip whose expected loss
  fits that cap and the daily expense cap, whose modeled improvement
  `E|z| - E|z - loss|` exceeds `minimum_improvement`, and whose temporary holding
  (original + q) passes `RiskGate.validate` at the endpoint and the midpoint, with
  `costs = 2 x expected loss`. One-share stock trims give precision for the last
  dollars.
- **Just in time.** With `remaining` minutes to the relevant entry cutoff and
  `capacity = expected_loss(paced) x 60 / cycle_seconds x fill_efficiency`, the burner
  waits while `needed <= jit_headroom x capacity x max(0, remaining - jit_reserve_minutes)`
  (`jit_headroom <= 1`). The burner spends early only what the remaining time could not
  absorb, and finishes in the last minutes. Pacing errs toward starting early:
  - `cycle_seconds` is `assumed_cycle_seconds` (20 s), or today's measured mean once 3
    cycles completed if that is slower. Measurements never speed the assumption up,
    because they omit the waits between cycles (scenario rebuilds, hedge optimizations).
  - `fill_efficiency` is realized / planned loss over today's transmitted cycles (IOC
    misses and partial fills), from the third cycle on, clamped to [0.25, 1].
  - An option clip is paced on the largest stock candidate and the stock window, so it
    is used only when the stocks alone could not absorb the need before their cutoff.

### Candidates and the cost model

- **Stocks.** Each `symbols` entry must be a defined, allowlisted, permitted, non-mandated
  STK contract (SPY is rejected). Sizes come from `quantities`, up to `max_shares` and
  the displayed bid and ask size. The cycle buys `q` at the ask and then sells `q` at
  the bid:
  `expected_loss = q x (ask - bid) + 2 x commission(q)`, where
  `commission(q) = max(minimum_commission, commission_share x q)`, capped at 1% of
  trade value. The cap matters for 1-share RKLB trims.
- **Options** (`option_burn: true`). Allowlisted, non-mandated options with
  `ask - bid <= option_max_spread`, fresh quotes and Greeks, in sizes of
  1..`option_max_contracts`:
  `expected_loss = q x 100 x (ask - bid) + 2 x max(minimum_commission, commission_option x q)`.
  Options are tradable only until close minus `option_cutoff_minutes`, so option
  entries are confined to roughly close-20 to close-17.
- **No new shorts.** Only instruments whose ORIGINAL holding is >= 0 are used. Each cycle
  buys first and then sells, so an exit never needs borrow (Moomoo has no borrow data).
- **Quote freshness.** Quotes must be live and no older than `quote_age_seconds`
  (default 3 s), with spreads inside the ordinary execution spread limits (options too).
  A locked book (ask == bid) is rejected: it would price a round trip at commissions only.
  The risk gate checks burner legs against the same `quote_age_seconds` (when longer than
  `risk.stock_quote_age`). On Oct 27, Moomoo's after-hours quotes ran 2-6 s old, and the
  5 s stock limit refused 17 burner exits.
- **Paper books outside the feed** (`cross`, `fill_spread`, `check_displayed_size`). IBKR
  paper fills extended-hours stock orders at its own book, not Moomoo's.
  - **What happened on Oct 27:** every IVV buy filled at 774.40 and every sell at 773.31,
    whatever the size or limit, while Moomoo showed about 773.2/773.3.
  - **`cross` (per symbol):** moves burner limits that many dollars beyond the feed's
    touch, so they reach paper's book. The fill still comes at the book's price.
  - **`fill_spread` (per symbol):** sets the spread a cycle is planned at, when it is wider
    than the feed's: `expected_loss = q x max(ask - bid, fill_spread) + 2 x commission(q)`.
    A cycle is then sized at what it really costs, not at a tenth of it.
  - **`check_displayed_size: false`:** drops the displayed-size limit. Paper filled 1,000 IVV
    at one price.

## When it may act

- `burner.mode != off`, on a scored session (never Day 0), with
  `close - window_minutes <= now < close - stock_entry_cutoff_minutes`, where `close` is
  the regular close. Option entries also need `now < close - option_entry_cutoff_minutes`.
- In the contest instead: `marks_fixed_at <= now < trading_end - stock_entry_cutoff_minutes`
  (16:16-19:56 ET), shares only, legs passing `post_market=True` to the order window (the
  hedger itself no longer trades after the close). Early closes come from the exchange
  calendar (the post-market session ends 4 hours after the close).
- Entry cutoffs are validated to leave at least 2 minutes before the ordinary order
  cutoffs (`stock_entry_cutoff_minutes >= policy.stock_cutoff_minutes + 2`, and the
  same for options), so every exit fits inside the normal `can_trade` window.
  RiskGate's order window is never relaxed.
- **Shadow.** In shadow (`burner.mode: shadow`, or app mode SHADOW) the burner only
  writes throttled `burn_decision` records and exposes its state.
- **Paper.** It trades only when `burner.mode: paper` (which requires
  `admissibility_source`) AND the app is in PAPER. The "Enable paper trading"
  confirmation says so when the burner is configured for paper.
- **Only the burner spends surplus.** An enabled burner (shadow or paper) requires
  `policy.burn_surplus: false`, so the hedger never treats its own costs as spending
  the surplus during the day.
- **Deadlines at transmission.** Entry and exit legs re-check their cutoff (entry
  cutoff, exit deadline) at the moment of sending, after any lock or preview waits.
- **It waits (no new cycle) when any of these holds:**
  - execution is paused or a pause was requested, the broker is read-only, or a data
    wait is active;
  - an unresolved (SUBMITTING/UNKNOWN) intent, any other working intent or broker
    working order exists;
  - a hedge plan (`pending_target` or `pending_recovery`) is pending, or an
    optimization is in flight;
  - the latest risk check failed;
  - the closing target or the NAV estimate is unavailable (a NAV attribution lag means
    wait, not error);
  - the scenarios are stale (`last_scenario_revision != broker_event_revision` or older
    than 20 s; the burner then wakes the monitor loop for a rebuild);
  - quotes are too old, or the daily cycle limit (`max_cycles_per_day`) or
    `daily_expense_cap` is exhausted.

## The cycle

Cycles are strictly sequential. An entry and an exit are never working at once, and two
cycles never overlap.

Outside regular hours IBKR does not accept IOC (its outside-RTH flag excludes IOC), so in
the contest's pre/post-market sessions every share leg is a DAY limit at the same price,
flagged for outside regular hours, and the burner cancels whatever has not filled 3 s
(`DAY_LEG_SECONDS`) after sending it. That is the same fill-what-is-there leg; the cancel
is a `CANCEL` cycle event, and everything below (early Cancelled statuses, confirmations,
exits sized from fills) applies unchanged.

```
IDLE -> ENTRY: IOC buy q at the ask (tick-rounded with broker.price_tick)
     -> settle: entry intent terminal, its fills recorded (at least the broker-reported
        filled count), and broker position == original + f (bounded 5 s wait,
        then one reconcile; a failed reconcile is retried, never counted)
     -> an IOC that ended short of its quantity is confirmed first: wait 1 s, then
        download executions, completed orders and positions (an entry once, an exit
        twice before another exit is sized)
     -> f == 0: CLOSED (nothing to exit)
     -> EXIT: IOC sell f at the bid minus exit_collar_ticks[attempt] ticks, retried with
        the next collar until the net residual is 0; while the book is wider than the
        execution spread limits the exit waits, except in its last 30 s
     -> CLOSED, or RESIDUAL when exit attempts are exhausted or the stock order window is
        about to close (close - stock_cutoff - 15 s)
```

- **Exit size.** The exit is always sized from the confirmed fills, i.e. Σ
  `Store.effective_fills` for the cycle's order references, never from intent state.
  An IOC that fills partially ends CANCELLED, and late executions do not change that.
- **Early Cancelled statuses.** On error 202, ib_async marks an order Cancelled with the
  last known `filled` (often 0) and can deliver that before the execution report and
  the position update. Without the confirmation step above, the burner would close an
  entry as "unfilled" while shares land, or send a second exit and go short. Every
  short-filled entry is therefore confirmed by one broker download, and a short-filled
  exit by two, before the burner decides CLOSED or another exit attempt. If a download
  fails, it retries; at the close the cycle becomes RESIDUAL. A fill that still arrives
  after its cycle ended raises an error alert (it is ordinary inventory then).
- **Never past the close.** A leg that is still not final, or a settle download that keeps
  failing, finishes the cycle RESIDUAL at the close (after one download), so the hedger is
  never blocked into the next session. An uncertain leg from an earlier session that a
  clean reconcile no longer finds at the broker is closed as ABANDONED.
- **Wide books.** Entries and exits refuse a book wider than the execution spread limits
  (`execution.stock_spread_absolute/relative`), so a momentary gap cannot turn a planned
  few-dollar round trip into a large loss. An exit stops waiting 30 s before its deadline
  and sells at the bid rather than carry the inventory.
- **RESIDUAL.** A once-only alert (key `burner_residual/session/cycle`) states the residual
  by the recorded fills and the broker position change since the cycle started; the
  final result releases the hedge block, `pending_target` is cleared and
  `force_optimize` is set. The hedger and RiskGate then treat the leftover as ordinary
  inventory. The burner never liquidates silently.
- **Durable records.**
  - `burn_cycle` (insert-once, id = cycle_id): the plan, with NAV estimate, target,
    median/mean/p05/p95 gap, needed, expected loss, JIT inputs, candidate, scenario
    id, quote and the admissibility source.
  - `burn_cycle_event` (append-only): PLANNED, ENTRY_SUBMITTED, EXIT_SUBMITTED,
    EXIT_REFUSED, RECONCILE, REHYDRATED, CLOSED/RESIDUAL.
  - `burn_cycle_result` (id `result:<cycle_id>`): fills, VWAPs, realized loss, fees,
    `fees_pending`, latencies, cycle seconds and residual.
- **Realized loss.** `realized_loss = Σ signed x price x multiplier + Σ commissions`,
  i.e. net cash paid for net-zero inventory. A round trip that buys at the ask and sells
  at the bid pays the spread, a positive loss. With a residual, the residual is carried
  at its entry VWAP, so the figure covers the round-tripped part only. Commission
  reports can arrive after executions; the dashboard refreshes fees for results still
  marked `fees_pending`.
- **Restart and reconnect.** On start, non-final cycles are rehydrated from records plus
  effective fills. The service always restarts PAUSED. Once paper trading is
  re-enabled and the exit window still allows it, the open exit resumes. Otherwise the
  cycle becomes RESIDUAL at the exit deadline, with an alert.
- **Pause and data wait.** An operator pause or a data wait cancels working legs like
  any order and sends no liquidation. A once-only alert names the held quantity and the
  deadline.

## Interaction with the hedger and order plumbing

- **The hedger yields while a cycle is open.** `ExecutionManager.burn_open()` reads the
  store (a `burn_cycle` without a result). While it is true, `submit_slice`,
  `submit_package`, `_execute_pending`, `_setup_slice` and `optimize()` refuse or skip,
  and the add-on export reports `execution_busy`. `optimize()` also discards a solve if
  a cycle opened during it, since a completed round trip restores quantities while NAV
  moved. Between cycles the hedger runs normally and has priority: a pending hedge plan
  blocks new burns.
- **Burn legs** are ordinary `OrderRequest`s with `order_type: LMT`,
  `time_in_force: IOC` and `urgency: BURN`. The order reference stays
  `hedge:<intent_id>`. Purpose, cycle id, leg and attempt live in the leg's
  `execution_plan` record and are exposed on intent rows (`purpose`, `cycle_id`, `leg`,
  `attempt`, `time_in_force`).
- **`ExecutionManager.submit_burn_leg`** mirrors `submit_slice`'s durable sequence:
  intent and plan in one transaction, VALIDATED, fresh quote plus RiskGate re-check,
  `prepare_request`, `reserve_order_id`, SUBMITTING, `broker.submit`. An exception
  after SUBMITTING leaves the leg UNKNOWN and pauses execution; it is never resent.
- **Not routed through the hedge checks.** Burn legs never go through
  `feasible_slices`/`economic_check`. Those checks still reject exposure-free fee
  burning for the hedger.
- **Margin preview.** Only entry legs get the credit preview, under the shared
  one-per-minute limit. Exits are never blocked by preview state.
- **Reconcile lock.** Legs are transmitted under `service.reconcile_lock`, after checking
  `broker.reconciled`, so the 60 s reconcile cannot interleave with a send.
- **Other plumbing.**
  - `manage()` never reprices or cancels burn legs. The 15 s SUBMITTING safety rule
    still applies.
  - Temporary inventory is declared in `owned_inventory_ranges` and cleared at the end
    of the cycle.
  - Error 202 for an unfilled burn IOC raises no per-order alert.
  - The burner runs as its own task (1 s or event-driven). A burner fault sets its state
    to ERROR with an alert, but never hard-stops hedging.

## Storage and dashboard load

Hundreds of burn legs per session would otherwise make every broker event and every 2 s
dashboard poll scan the whole history. No schema columns change. Instead:

- **Per-event lookups are primary-key reads.** Execution plans have deterministic record
  ids (`plan:<intent_id>`); older plans are still found through a JSON-path fallback.
  `on_order` and `on_execution` read a single intent rather than every intent.
- **Finding a cycle's legs.** Burn-leg intent ids begin with the cycle id's first 24 hex
  digits, so the legs are an id-prefix match. The `orderRef` stays
  `hedge:` + 32 hex characters.
- **Bounded dashboard state.** `state()` returns the 250 most recent orders, fills and
  execution-quality rows; the bounded quality view reads only the related records and
  fills. CSV exports (`/api/export/orders`, `fills`, `execution_quality`) still return
  the complete history.
- **New index.** `records(category, account, at)` is created at startup if missing,
  including on an existing PostgreSQL database (`create_all` never adds indexes to an
  existing table).

## Operating it

1. **Configure.** The template's `burner:` block is set to `mode: paper` with
   `symbols: [SPCX, RKLB]` and the recorded `admissibility_source`. It trades only
   while the app itself is in paper (see automatic paper start in
   CONTEST_RESTRICTIONS.md). To watch first without trading, use the panel's
   "Shadow burner" button (or `mode: shadow`) and review the `burn_decision` records.
2. **Changing the configuration** changes `config_hash`, as any configuration
   change does.
3. **Dashboard panel.** "Closing surplus burner" shows mode, state and reason, the
   window, target, NAV estimate, gap statistics, the JIT pacing, the open cycle,
   today's totals (cycles, realized cost, pending fees, turnover, mean cycle time) and
   the last 20 cycles. Export every session's cycles from `/api/export/burn_cycles`.
4. **Operator switch.** `POST /api/burner {"mode": "off"|"shadow"|"paper"}` (the panel
   buttons) only downgrades new entries below the configured mode, and survives
   restarts and configuration edits (it can only ever downgrade). An open cycle still
   restores its inventory.
   The switch never changes the control epoch, so automatic data-wait resume is
   unaffected.
5. **Event caution.** An approved event caution with "Burner off for this session"
   (Overview > Event risk today; docs/AI_OPERATOR_FEATURES.md) turns new entries off for
   that session only ("Burner off for this session (approved event caution)"). It only
   lowers the mode, leaves the switch above untouched and still lets an open cycle sell
   back. An approved caution also widens the forecast moves behind the median closing gap;
   the burner waits for a forecast rebuilt with the approved factors before it acts.

## Configuration (`burner:`)

| Field | Default | Meaning |
| --- | --- | --- |
| mode | off | off / shadow / paper |
| admissibility_source | "" | Required for paper: who permitted deliberate expense, and when |
| symbols | () | Allowlisted stocks to round-trip (never SPY) |
| quantities / max_shares | 1,5,10,25,50,100,200 / 200 | Stock size ladder (positive, sorted) |
| option_burn, option_max_contracts, option_max_spread | false, 5, 0.20 | Optional option round trips |
| window_minutes | 20 | Active from close minus this |
| stock_entry_cutoff_minutes / option_entry_cutoff_minutes | 4 / 17 | No new entries after close minus these |
| start_threshold / stop_threshold | 25 / 5 | USD hysteresis on the spend quantile of the gap |
| spend_quantile | 0.5 | Gap quantile spent toward (contest: 0.4); 0.05-0.5 |
| post_close_basis | worst_case | After the close: `worst_case` spends only the gap at the least favorable option prices; `quantile` spends toward spend_quantile of the samples; `prediction` (contest, from Oct 26) spends down to the prediction less post_close_margin |
| post_close_margin | 50 | USD kept under the predicted statement NAV by `prediction` |
| require_pnl_stream | false | Act only on the settled 1-second IBKR P&L stream NAV (contest: true) |
| settle_seconds | 0 | Wait after a cycle that filled before the next decision (contest: 8) |
| bias / bias_source | 0 / "" | Signed model-vs-official calibration |
| jit_headroom / jit_reserve_minutes | 0.5 / 1 | Just-in-time pacing (headroom <= 1) |
| assumed_cycle_seconds | 20 | Minimum assumed cycle time (measurements only lengthen it) |
| minimum_improvement | 0.25 | USD modeled improvement per cycle |
| daily_expense_cap | null | Optional USD cap on realized burn per session (commissions not yet reported count at their estimate) |
| max_cycles_per_day / max_exit_attempts | 300 / 4 | Transmitted cycles; exit legs per cycle |
| exit_collar_ticks | 0,1,2,5 | Ticks below the bid per exit attempt (non-decreasing) |
| quote_age_seconds | 3 | Maximum quote age for any burn leg |

## Limits and calibration

- **Capacity is modeled, not guaranteed.** Throughput is serial: each cycle needs an
  entry, settlement, an exit and a fresh scenario frame from the monitor loop. Expect
  tens to a few hundred stock cycles per window. In a 17-minute window at 100-200
  shares, that is roughly `q x spread + about $2` per cycle.
- **IBKR paper fills differ from Moomoo quotes.** IBKR paper fills IOC orders at its own
  simulated top of book, while quotes come from Moomoo. A leg priced off a 1-3 s old
  Moomoo book can miss or partially fill. Measure the fill ratio, the
  fill-versus-quote gap and the commission schedule on paper with small size before
  relying on capacity.
- **The target is only as good as the previous close.** If the previous close is an
  API capture rather than the statement NAV, the target can be off by dollars. Treat
  that as a `bias` input, calibrated from the `close_marks` pre-close records against
  the official statement gaps.
- **Daily capacity.** After the close (Oct 26, paper): IBKR's simulated after-hours SPCX book
  sat at 147.70/147.75, so a full 200-share cycle cost $12 ($10 spread, $2 commissions). Entries
  priced at Moomoo's ask filled only when it reached IBKR's 147.75 (11 of the first 27); RKLB
  never filled. Cycles ran about 8 s apart, so `max_cycles_per_day` (misses count) binds long
  before 19:56: 300 cycles x 40-70% x $12 is about $1,450-2,500 an evening. The contest config
  uses 400 shares and 600 cycles (about $5,000-9,000). In the Oct 8-21 replay a $1,440 capacity
  left a $559 mean daily miss, $3,600 left $296. The plan sizes each cycle with Moomoo's spread
  (2 cents) while IBKR paper charges 5, so a full final cycle can overspend by about $6-12,
  inside the $50 margin.
- **Recovery accounting.** Burn round trips never count toward recovery inventory or
  recovery P&L.
- **Burning is irreversible.** Remaining surplus after the entry cutoff is an accepted
  outcome. The burner never chases with market orders or closing-auction orders.
- **SimBroker emulation.** The simulator fills an IOC at the touch, up to displayed
  size, with no price improvement, and then cancels the rest. Rehearsals can script
  partial or zero fills with `SimBroker.ioc_fill_caps`.
