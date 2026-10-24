# Running the contest from the dashboard

Everything below happens in the browser at http://127.0.0.1:8765, except
double-clicking the launcher and the trades the app cannot place for you (the
seven mandated legs and any leftover non-contest positions, both done in TWS).

## Start and stop

- **Start:** double-click **Start Contest.command**. It checks the contest
  configuration, starts the local PostgreSQL database, refreshes the daily price
  history from Moomoo (quote-only; skipped after 2.5 minutes if OpenD does not
  answer, and during 15:35-16:10 ET), keeps the Mac awake and opens the dashboard.
  Leave its window open; you may minimize it. If it says port 8765 is in use,
  close the other window (Start Practice or an earlier Start Contest) first.
- **Stop:** close that window (or press Control-C in it). The app pauses
  trading, cancels its own working orders and shuts down cleanly. Filled
  positions stay in the account.
- Keep TWS (Paper) logged in with the API enabled on port 7497 and
  **Read-Only API unticked**, and keep Moomoo OpenD logged in.
- **Automatic confirmation and start (Oct 23):** the contest configuration confirms itself once
  IBKR has verified every contract and either you confirmed one for the same account and seven legs
  before (a settings change), or the paper account holds exactly the seven configured legs. No
  button press is needed; **Confirm contest configuration** stays for an early review. With Auto
  paper ON, paper trading starts by itself at each session's trading start
  (04:00 ET pre-market), also after a pause or a safety stop the day before, through every
  activation check. To stay paused through the next session, turn Auto paper OFF.
- **Lost internet or on battery:** keep the app running. When the connection drops,
  trading waits (mode WAITING_DATA) and restarts by itself once IBKR and fresh quotes are back
  and every paper check passes; an order cut off by the drop is closed after a clean
  reconcile. Nothing to press. Closing the lid sleeps the Mac: trading stops until it wakes.

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
| Contest configuration | Confirms by itself once IBKR has verified every contract and holds the seven configured legs (or a configuration with the same legs was confirmed before). Until then, enter the legs the Mandated legs row lists; **Confirm contest configuration** confirms early. |
| Quotes | Moomoo quotes are live 09:30-16:00 ET. Outside those hours this row is a !, not a problem. |
| Price history | Press **Refresh price history now** if it failed or is old. The app also refreshes it by itself between 08:30 and 09:29 ET. |
| Previous official close | The scored close is the IBKR statement NAV. The app records its predicted value after 20:05 ET (! until you import the statement); enter the statement "Ending Value" in Settings & alerts > Import an official closing NAV each morning. Missing blocks paper trading. |
| Power | On battery or lid closed: plug in the Mac and keep the lid open through 20:10 ET; a sleeping Mac misses the 16:00 closing capture. Advice only. |
| Statement import | Not set up: double-click Setup IBKR Flex.command (Flex token and query from Client Portal). |
| Automatic paper start | Shows whether it is on and what it waits for. |
| Trading | The current mode and why it waits, if it does. |

## Automatic statement import (once)

In Client Portal (paper login), Performance & Reports > Flex Queries:
1. Turn on **Flex Web Service Configuration** (gear icon) and generate a token (longest expiry).
2. Create an **Activity Flex Query** with sections Net Asset Value (NAV) in Base, Change in NAV and
   Open Positions; Format XML, Period Last Business Day. Note its Query ID (info icon).
3. Double-click **Setup IBKR Flex.command** on the Mac and enter both. They stay on this Mac,
   owner-only. Settings & alerts > Statement prices check shows each import.

**Locked token (error 1025, "Too many failed attempts").** IBKR locks a token after a burst of failed
requests. On Oct 23 this happened when an earlier history script asked repeatedly for days before the
account existed. The app then asks only once an hour and says so on the Statement import row. If
it still fails in the morning, generate a new token (same page) and run Setup IBKR Flex.command
again. Generate a new one after the contest in any case if the token was ever pasted anywhere.

## Day 0 (Wed Oct 21), once

1. Before 09:30 ET: in TWS, untick Read-Only API. Start the app.
2. At the open: close the leftover positions in TWS (Account holdings row).
3. When the IBKR row is ✓: optionally press **Confirm contest configuration**; otherwise it
   confirms by itself once the seven legs are held.
4. Enter the seven legs in TWS (Mandated legs row shows what is left).
5. Once all seven are held, automatic paper start begins hedging when the
   checks pass. Nothing else to press.
6. Keep the app running past 16:25 ET: it records the Day 0 closing NAV (the
   unscored baseline) at 16:00 and checks it again at about 16:20.

## Every scored day (Oct 22-23, Oct 26-30, Nov 2-4)

- **Before 09:30 ET:** the app can stay running overnight. The instructor scores the IBKR
  statement NAV. Once **Setup IBKR Flex.command** is done, the app records each statement NAV
  (Day 0 included) by itself after midnight (Today row Statement import). Otherwise enter yesterday's
  statement "Ending Value" (Settings & alerts > Import an official closing NAV), and once the Oct 21
  (Day 0) one; until then the Previous official close row warns.
- **About 08:45 ET:** with AI on, **Event risk today** (Overview) lists the day's known events
  (earnings, Fed, CPI/jobs data, launches) with their sources and may propose a forecast caution.
  Review it, then press **Approve caution** or leave it; nothing applies until you approve, and an
  approval expires at that day's close.
- **09:30 onward:** hedging resumes by itself. Watch the Today panel and
  Settings & alerts > Alerts & required actions.
- **16:16-19:56 ET:** the closing surplus burner may buy and sell SPCX/RKLB shares to spend a
  surplus of the predicted statement NAV (Overview > Closing surplus burner; docs/CLOSING_BURNER.md).
  **Do not pause or restart between 15:40 and 19:58 ET**: a pause strands an open round trip's
  shares overnight. To stop new burn trades only, press
  **Burner off**; any open round trip still sells back what it bought.
- **Any time:** if paper trading is not on and the Today panel is unclear, type the
  question in **Why isn't it trading? Ask the app** (Overview). The answer is advice;
  the Today rows and the app's checks decide.
- **The scored close (statement NAV):** cash after the post-market session plus positions at the
  closing marks (shares at the 16:00 close, options at their closing midpoint, SPY options at
  16:15). After 16:00 the market no longer moves it, so the app stops hedging then; only the burner
  trades after the close. From 16:16 the dashboard's NAV is this predicted statement NAV, and from
  20:05 ET it is recorded as the day's provisional close; the imported statement replaces it.
- **04:00-09:30 ET (pre-market):** paper trading continues with shares only (SPCX, RKLB and any
  added hedge stocks such as VOO/IVV), with limit orders flagged for outside regular hours; options
  trade only 09:30-15:45. The forecast runs to the 16:00 close. With Auto paper ON the app starts
  trading at 04:00 once every check passes.
- **After about 20:21:** read **Day-end miss explanation** (Overview, under the scoring
  ledger): how far the official close was from the target and why. Nothing to press.

## After the contest (Nov 4)

Keep the app running until about 20:25 ET on Nov 4 (post-market trades book to that day's
statement). On Nov 5, import the Nov 4 statement Ending Value as on every other morning. Download the records with the
"Export … ↗" links: scores, burn cycles and day reports on Overview, orders, fills and
execution quality on Trades, decisions on Decisions.

## AI operator features

Optional and advice only: they never place, size, approve or block a trade and never
change a setting. One switch, **Enable AI** (Decisions > AI supervisory review), turns
all of them on or off; trading never waits for them.

- **Why isn't it trading? Ask the app** (Overview, under Today): type a question, e.g.
  "Why is paper trading not on?", and press **Ask**. With AI on, Claude Sonnet answers
  from the app's own status (Today rows, mode, waiting reason, burner, alerts, latest
  decisions, risk checks) and this guide, and shows what it relied on. With AI off, or if
  the AI fails or 60 questions were asked in the last hour, the answer comes straight from
  the Today rows. One question every 10 seconds. Then do what the Today rows say.
- **Day-end miss explanation** (Overview, under the scoring ledger): about 16:26 ET on
  each scored day the app splits the miss (official close minus target) into stock and
  option moves, trades, commissions, the burner and an "Unexplained" remainder, so the
  lines add up to the official numbers. The numbers are always shown. With AI on, a short
  explanation, items worth checking and up to three suggested settings appear below them;
  the suggestions are never applied, so change a setting only with its own button.
  **Recompute now** picks up late commission reports or a corrected close, and computes a
  day shown as "(not computed yet)" in the date list; a large Unexplained line usually means
  an option was marked oddly or close marks are missing.
- **Event risk today** (Overview, under the latest hedge decision): about 08:45 ET on session
  days (or **Refresh event calendar**, at most 6 per day) a web search lists known events for
  SPCX, RKLB, SPY and the market until the next open, with links. The app proposes one caution per
  symbol: ×1.25 or ×1.5 widens that symbol's modeled price and implied-vol moves for this session
  (SPY also covers index ETF hedges), plus "Burner off for this session" for a late-day Fed or
  similar event. Review the list, adjust the choices, then press **Approve caution** (it asks you
  to confirm) or leave everything at ×1.0. Nothing applies until you approve; the approval ends at
  the session close by itself, and **Clear caution** removes it earlier. The 3% stress limit and
  every hard check are unchanged. With AI off no calendar is fetched; you can still approve by
  hand.

## Where things are

- **Overview:** Today, Ask the app, NAV and score cards, risk envelope, latest hedge decision,
  event risk today, closing surplus burner, official scoring ledger, day-end miss explanation.
- **Positions & risk:** market data monitor, actual holdings, Greeks and risk checks.
- **Trades:** execution quality, orders, fills and fees.
- **Decisions:** hedge decision history and the optional AI supervisor (advice only).
- **Settings & alerts:** mandated holdings, closing NAV recorder, strategy
  settings, official close import, alerts.
