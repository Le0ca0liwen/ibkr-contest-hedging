# Running the contest from the dashboard

Everything below happens in the browser at http://127.0.0.1:8765, except
double-clicking the launcher and the trades the app cannot place for you (the
seven mandated legs and any leftover non-contest positions, both done in TWS).

## Start and stop

- **Start:** double-click **Start Contest.command**. It checks the contest
  configuration, starts the local PostgreSQL database, refreshes the daily price
  history from Moomoo (quote-only), keeps the Mac awake and opens the dashboard.
  Leave its window open; you may minimize it.
- **Stop:** close that window (or press Control-C in it). The app pauses
  trading, cancels its own working orders and shuts down cleanly. Filled
  positions stay in the account.
- Keep TWS (Paper) logged in with the API enabled on port 7497 and
  **Read-Only API unticked**, and keep Moomoo OpenD logged in.

## The control bar (top of every page)

| Button | What it does |
|---|---|
| Enable paper trading | Starts paper hedging now, after the same checks as the automatic start. Shows "Paper trading on" while trading. |
| Pause trading | Stops all new orders and cancels the app's working orders. Positions stay. Cancels a pending automatic start until the next launch: with Auto paper ON, a restart starts paper again once the checks pass. To stay paused across restarts, also turn Auto paper OFF. |
| Reconcile | Re-reads positions, orders and fills from IBKR. Safe at any time. |
| Start shadow | Watch-only: the app calculates hedges but sends nothing. Cancels a pending automatic start. |
| Auto paper: ON/OFF | ON (the contest default) starts paper trading by itself once every check passes, now and after each launch. Turning it OFF never stops trading that is already running; use Pause for that. |

## Today: readiness (top of Overview)

Each row is ✓ (fine), ! (attention) or ✗ (blocks paper trading), with the
reason in plain words. The badge counts the ✗ rows.

| Row | What to do when it is not ✓ |
|---|---|
| Database | Close the app and start it with Start Contest.command (paper trading needs PostgreSQL). |
| IBKR | Open TWS (Paper) with the API on port 7497. If it says reconciliation stops on non-contest positions, see the next row. |
| Account holdings | Close the listed non-contest positions in TWS. |
| Mandated legs | Enter the listed BUY/SELL quantities in TWS (Paper). The app cannot place these seven legs. |
| Contest configuration | Press **Confirm contest configuration** (enabled once IBKR has verified every contract), check the seven legs in the dialog, confirm. Needed once per configuration. |
| Quotes | Moomoo quotes are live 09:30-16:00 ET. Outside those hours this row is a !, not a problem. |
| Price history | Press **Refresh price history now** if it failed or is old. The app also refreshes it by itself between 08:30 and 09:29 ET. |
| Previous official close | Enter the IBKR statement NAV (Settings & alerts > Import an official closing NAV). Missing blocks paper trading; "API capture only" is a reminder. |
| Automatic paper start | Shows whether it is on and what it waits for. |
| Trading | The current mode and why it waits, if it does. |

## Day 0 (Wed Oct 21), once

1. Before 09:30 ET: in TWS, untick Read-Only API. Start the app.
2. At the open: close the leftover positions in TWS (Account holdings row).
3. When the IBKR row is ✓: press **Confirm contest configuration**.
4. Enter the seven legs in TWS (Mandated legs row shows what is left).
5. Once all seven are held, automatic paper start begins hedging when the
   checks pass. Nothing else to press.
6. Keep the app running past 16:25 ET: it records the Day 0 closing NAV (the
   unscored baseline) at 16:00 and checks it again at about 16:20.

## Every scored day (Oct 22-23, Oct 26-30, Nov 2-4)

- **Before 09:30 ET:** the app can stay running overnight. Check the Today panel.
  Enter yesterday's IBKR statement NAV when it is published (Settings & alerts >
  Import an official closing NAV; "$1,234,567.89" is accepted as printed).
- **09:30 onward:** hedging resumes by itself. Watch the Today panel and
  Settings & alerts > Alerts & required actions.
- **15:40-15:56 ET:** the closing surplus burner may buy and sell SPCX/RKLB shares
  to spend a forecast surplus (Overview > Closing surplus burner). **Do not pause
  or restart between 15:40 and 16:25.** To stop new burn trades only, press
  **Burner off**; any open round trip still sells back what it bought.
- **16:00 and about 16:20:** the closing NAV is recorded automatically
  (Settings & alerts > Automatic closing NAV recorder). A DIVERGED warning means:
  import the statement NAV for that day.

## After the contest (Nov 4)

Keep the app running until about 16:25 ET on Nov 4, then import any statement
NAVs still marked "API" in the scoring ledger. Download the records with the
"Export … ↗" links: scores and burn cycles on Overview, orders, fills and
execution quality on Trades, decisions on Decisions.

## Where things are

- **Overview:** Today, NAV and score cards, risk envelope, latest hedge decision,
  closing surplus burner, official scoring ledger.
- **Positions & risk:** market data monitor, actual holdings, Greeks and risk checks.
- **Trades:** execution quality, orders, fills and fees.
- **Decisions:** hedge decision history and the optional AI supervisor (advice only).
- **Settings & alerts:** mandated holdings, closing NAV recorder, strategy
  settings, official close import, alerts.
