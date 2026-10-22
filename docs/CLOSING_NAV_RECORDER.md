# Automatic IBKR closing NAV recording

The app requests IBKR `NetLiquidation` at the exchange close for Day 0 and every
configured scoring session. It uses the exchange calendar, including early
closes, and runs independently of paper/shadow/paused mode, quote quality and
optimization. No dates are hardcoded in this workflow.

Defaults are enabled, a 180-second capture window and a 15-second retry interval.
These are validated through `closing_capture` in the contest manifest:

```yaml
closing_capture:
  enabled: true
  window_seconds: 180
  retry_seconds: 15
  late_capture_minutes: 20   # 0 disables; otherwise 18-60
```

The recorder requests a fresh account summary and requires a new NAV event for
the configured paper account. It does not reuse a cached summary, a pre-close
value, another account or moomoo data. Raw NAV includes accrued interest and
broker charges; nothing is subtracted or added again.

Observations enter the revisioned NAV ledger with scheduled-close and actual
observation timestamps and API provenance. The next session uses the recorded
previous close and sourced cash benchmark or separately labeled approved forecast.
API captures are marked as awaiting statement reconciliation. Importing the
organizer's corrected IBKR statement revises the value and adjacent scores.

## Statement first

For the SPCX/RKLB/SPY contest (`contest_rules: spcx_rklb_spy`) the operator's instruction of
Oct 21 applies instead: the IBKR API closing NAV is the official close. Its API rows (bell capture
and, when taken, the post-close observation) are final, not provisional: no statement import is
needed, no import warning is raised, and the dashboard, readiness row and AI prompts call the API
close official. A manual entry (Settings & alerts > Import an official closing NAV) only corrects a
missed or wrong capture, and then wins over the API rows for that day. Post-market trading and NAV
changes count too (operator instruction, Oct 22), so the official close is an **end-of-day**
NetLiquidation reading 5-20 minutes after the post-market session ends (regular close + 4 h, i.e.
20:05-20:20 ET), stored as `IBKR API end-of-day observation (...; official close)`. Being the latest
API row, it replaces the 16:00 and 16:20 readings, which are interim. It is taken even if both of
those were missed, and skipped for a day whose close was entered by hand. A missed window is
recorded (MISSED, phase `end_of_day`) and raises one alert asking for a manual entry. The day-end
report waits for it (20:21 ET). In this contest a missed bell
capture gets one more try: the post-close observation (16:20-16:25 ET) is recorded as the close,
with "; bell capture missed" in its source, unless it differs from the NAV of the last broker
snapshot taken before the close by more than `late_capture_max_delta` x initial NAV. The day is
marked MISSED (manual entry required) only when that also fails. The rest of this section describes
the statement-first behaviour that other contest configurations (e.g. practice) keep.

The instructor scores the official IBKR Custom Statement NAV. The bell capture can
differ from it by roughly $300-1,200: API account values refresh only about every
three minutes and SPY options mark at their 16:15 close. Each ledger row is therefore
classified by its stored source, without a schema change: sources starting with
`IBKR API` or `SYNTHETIC simulator` are provisional **api** captures; anything else
(dashboard entry, CSV import of the statement) is **statement**. For each day the
latest statement row is used regardless of insertion order; without one, the
latest api row is used. Score summaries report each day's `provenance`.

After a recorded bell capture, the recorder takes one more fresh NetLiquidation
observation `late_capture_minutes` after the close (within the following five
minutes) and stores it as a new api row, `IBKR API post-close observation (after
16:15 options close; ...)`, which then sets the next target. It is skipped when a
statement exists for that day, also if one arrives during the request, and outside the contest it
is never taken for a missed bell capture: that day still requires the statement.

The NAV request has its own IBKR request id, so it does not wait for the reconcile lock (a
reconnect that re-qualifies every contract can hold that lock for minutes), and each attempt is
limited to 30 s so a hung request cannot keep the recorder from its window.

The dashboard and supervisor context show `previous_close_provenance` for today's
target. When it is `api`, a warning is recorded once per session (restarts
included): "Import yesterday's official IBKR statement NAV before the open".

An existing close is retained, including when a report arrives during the API
request. Restarts do not duplicate captures or overwrite a statement. Failed
requests retry only within the capture window. A missed window creates a durable
alert requesting a statement import; starting later does not substitute today's
NAV for a missed close.

Keep the Mac awake, service running and Paper TWS authenticated at the close.
The launcher prevents idle sleep; laptop closure or TWS shutdown can still
prevent capture. Settings shows recorder status and next scheduled close.
Disconnected broker or failed storage cannot create a fabricated closing value.

The operator supplied October 7's statement NAV of $995,294.19. Its import gives
an estimated October 8 target of $995,386.81 under the approved approximate rate.
Official scoring still requires the actual daily rate. Limits remain unchanged.

Broker initialization and reconciliation share a lock. Contract qualification
has a 15-second deadline. Required-contract failure disconnects and clears readiness
for retry. An unheld optional candidate that times out is quarantined: its quotes
are excluded from optimization and its orders are rejected. Mandatory contracts,
all filled holdings, working orders and their option underlyings must qualify.
Native contract IDs, types, currency and option identity must match. Closing-summary
subscriptions and handlers are removed after requests.
