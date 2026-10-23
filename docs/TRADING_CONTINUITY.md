# Continuous paper operation and recoverable waiting

Paper execution now distinguishes a temporary input interruption from a manual
or safety stop. `WAITING_DATA` retains the operator's previously enabled paper
or protected-setup intent. No new order is allowed while waiting. The dashboard
shows the reason and whether automatic resumption is pending.

## Investigation

The October 9 operational alerts recorded working-order checks against stale ITA
and UAL quotes at about 11:04 and 11:05 Eastern. The old service cancelled orders
and permanently entered PAUSED, even when data recovered. Successful paper
activation also retained a prior “Paused — all cancellations confirmed” message.

Two additional sources of unnecessary interruption were found:

- Order management used the quote dictionary captured before optimization or
  margin preflight. A multi-second solve could make that dictionary stale even
  though the collector had already published fresh quotes.
- Quote coverage could change between an optimizer frame and execution planning,
  producing a raw contract-ID KeyError. The Moomoo adapter could omit a quote
  because its last-trade timestamp was slightly ahead of the client clock, even
  when a verified executable book had a valid callback receipt timestamp. Archived
  data showed sub-second vendor/receipt timestamp differences.

## Recovery behavior

On a known data/connection interruption during enabled PAPER or SETUP:

1. Block submissions immediately; clear the obsolete target and recovery plan.
2. Retain the requested mode, account configuration hash, profile and operator
   action generation. Record a continuity event.
3. Request cancellation of system-owned working orders where possible. Apply any
   fills that race with cancellation. Keep filled holdings.
4. Continue broker event processing, recording and the regular monitoring loop.
   Connection retries back off; paper activation retries are rate limited to
   fifteen seconds. No full account request is made for every quote.
5. When quotes recover, run the full activation preflight again: reconcile
   positions, completed/open orders and executions; require no working or uncertain
   orders; rebuild scenarios from the current complete book; verify the paper
   account, qualified contracts, PostgreSQL durability, mandate, score baseline,
   approved benchmark, freshness and risk. A current risk breach still requires
   an independently valid corrective target and first execution slice.
6. Resume the previously requested mode only after all checks pass. Record RESUMED
   and discard the old decision. Resume never means retrying an uncertain order.

Only explicitly recognized data/network failures receive this behavior. Invalid
configuration, unsupported execution, durable event processing failure, unknown
order state and mandate/working-order risk failures continue to block execution.
Unknown submissions remain unresolved until broker evidence resolves them.

### Internet or IBKR outages (Oct 23)

The Mac may run on battery or lose its internet connection during the day. TWS then keeps the
API socket open but reaches nothing. On the Oct 22 19:50-21:29 outage, TWS answered 2151
"Positions info is not available yet" for every download. Recognized as the outage, such
errors now wait instead of stopping trading:

- **TWS's own link messages.** 1100/2110 ("connectivity lost") enters the data wait at once.
  1101/1102 ("restored") forces a full reconcile before any decision, and after 1101 (data
  lost) also renews the 1-second P&L stream (and IBKR market data when that is the provider).
  The broker tracks the link (`link_up()`); a complete download also proves it is up.
- **Errors during an outage.** A network error (`OSError`), an IBKR "unreachable" code (502,
  504, 1100, 1300, 2110, 2151, 10182), or any error while the link is down enters or keeps
  the data wait. Paper resumes by itself through the full activation preflight above once
  IBKR answers again.
- **Errors while already waiting.** Trading is stopped in a wait, so an unexpected error there
  (for example contract qualification failing on reconnect) keeps the wait and its automatic
  resume instead of ending it in a pause. While IBKR is reachable, an unexpected error during
  PAPER still stops trading.
- **An order cut off by the outage.** A submission that failed because the socket was down
  (Mac asleep) or went unacknowledged is UNKNOWN. When a complete download (open orders,
  today's completed orders and executions) still does not list it 2 minutes
  (`UNKNOWN_SETTLE`) after it was created, it never reached IBKR or has ended. It is closed
  as ABANDONED with an alert (positions remain the record), so it no longer blocks the restart
  for the rest of the day.
- **Moomoo.** The quote collector reconnects to OpenD and resubscribes every 10 s on its own.
  Until fresh quotes arrive the resume waits on stale quotes.
- **Power.** Start Contest keeps the Mac awake while the app runs (`caffeinate`), on battery
  too, but closing the lid still sleeps it. On waking, the app refreshes orders and positions
  from IBKR (SLEPT) and resumes as above. A
manual pause, shadow/profile change or configuration change invalidates automatic
reactivation. An operator pause during a slow preflight also wins: the action
generation is checked before enabling execution, and submission checks its paused
flag again after a credit preview.

Automatic resumption is process-local. Restarting the app begins paused and
requires normal activation; no restart implicitly arms mandatory purchases.
The configured setup deadline and final contest close remain enforced. After the
last scoring close the service retains holdings and avoids attempting a nonexistent
next-close scenario. The independent closing NAV recorder continues running.

## Quote and decision integrity

Stock/option/Greek freshness limits are unchanged. Current broker quotes are
fetched for working-order checks and management after optimization. Projected
working-order holdings are retained in scenario screening. Missing optional
quote coverage invalidates the old proposal for a fresh solve; missing held-asset
quotes block execution.

Moomoo remains data-only. The live-book permission, streaming-update count,
contract identity, source/receipt timestamps and model-calibration checks remain
required. A last-trade timestamp is not substituted for a verified book timestamp.
An unverified future trade remains unusable; an old book is never restamped or
made live merely because the collector writes a new file.

## Validation

Regression and fault tests exercise quote loss/recovery through the actual monitor,
current quotes after an eight-second simulated optimization, cancellation before
reactivation, unresolved submissions, operator pause during reconciliation and
credit preview, mandate/configuration changes, NAV attribution lag, raw quote
coverage changes, and slightly skewed last-trade timestamps. Tests use synthetic
brokers and isolated databases; they do not submit IBKR orders.

Validation on October 9: all 283 regression tests passed, including the isolated
PostgreSQL integration test and 22 continuity tests. Lint, formatting, JavaScript
syntax and diff checks passed for the changed application and test code.

Continuous operation means monitoring and safe automatic recovery. It does not
mean orders will be submitted without valid market data, a beneficial hedge,
available liquidity or a resolved broker state. Keep the Mac awake and TWS/OpenD
connected, and inspect the explicit waiting/stop reason when a dependency fails.
