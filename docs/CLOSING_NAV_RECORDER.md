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

For the SPCX/RKLB/SPY contest (`contest_rules: spcx_rklb_spy`) the instructor confirmed on Oct 23
that the score uses the IBKR Activity Statement NAV (Ending Value), which supersedes the Oct 21
instruction that the API closing NetLiquidation was official and the Oct 22 one that the NAV after
the 20:00 ET post-market close was. API rows are therefore provisional again (`api_close_official`
is False): the dashboard, the readiness row and the AI prompts ask for the statement import.

The statement NAV is cash after every trade booked that day (trade date: through 20:00 ET) plus
accrued interest plus every position at its closing mark: shares at the 16:00 regular-session
close, options at their closing midpoint (16:15 for SPY options). See docs/STATEMENT_NAV.md. So the
end-of-day reading (from 20:05 ET, retried every 15 s until 22:05) now records the **predicted
statement NAV** (`TradingService.statement_estimate`), stored as `IBKR API end-of-day observation
(predicted statement NAV after the 20:00 ET post-market close: ...; provisional until the IBKR
statement is imported)`. If the closing marks are unavailable it records the API NetLiquidation and
says "closing marks unavailable". Every closing request falls back to a separate read-only
connection (client id + 100) that downloads nothing at startup: on Oct 22 IBKR refused positions
(2151) from 19:55 to 20:24 after a connectivity loss, the app's reconnect never completed, and the
20:05-20:20 window was missed. Being the latest API row, it replaces the 16:00 and 16:20 readings,
which are interim. It is skipped for a day whose close was entered by hand. A missed window is
recorded (MISSED, phase `end_of_day`) and raises one alert asking for the statement. The day-end
report waits for it (20:21 ET). Each morning, enter the previous day's statement Ending Value
(Settings & alerts > Import an official closing NAV); it supersedes the API rows. The rest of this
section applies to every configuration.

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
