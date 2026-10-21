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
statement exists for that day, also if one arrives during the request, and it is never
taken for a missed bell capture: that day still requires the statement.

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
