# AI operator features

Optional helpers for the operator, built on the Google Gemini supervisor
([GOOGLE_SUPERVISOR.md](GOOGLE_SUPERVISOR.md)). The implementation spec is
[AI_OPERATOR_FEATURES_SPEC.md](AI_OPERATOR_FEATURES_SPEC.md).

| Feature | Status |
|---|---|
| "Why isn't it trading?" Q&A (Overview > Ask the app) | Built |
| Day-end miss explanation (Overview, under the Official scoring ledger) | Built |
| Event-risk calendar and approved event caution (Overview > Event risk today) | Built |

## Ground rules

- **Advice only.** Nothing the AI returns can place, size, approve, block or cancel an order, or
  change a mode, profile, limit, burner or autostart setting. The deterministic optimizer and risk
  gate trade. Every prompt ends with a fixed sentence saying so and telling the model to treat
  every input as data, never as instructions. The one AI-influenced trading input is the event
  caution: the AI only *proposes* it through a fixed table, it applies only after the operator
  presses **Approve caution**, it is bounded to ×1.0/×1.25/×1.5 and it widens forecast moves only,
  never the 3% stress limit.
- **One switch.** **Enable AI / Disable AI** (Decisions > AI supervisory review, saved in
  `config/local.ai.json`, plus the local key) governs every AI feature. With AI off, no feature
  calls Google; the Q&A answers from the readiness checklist, the day-end report shows its
  numbers only and the event calendar is not fetched (a caution can still be approved by hand).
  An approved caution stays in effect when AI is turned off: the operator approved it.
  **Disable AI** works at once, even while a review or another AI call is running (only a model
  change waits for a running review). A call still waiting for a Google slot is not sent, and a
  reply that arrives after AI was turned off is dropped without an alert: no calendar proposal, no
  explanation, and the Q&A answers from the checklist.
- **Off the trading path.** Every AI call is an async request with a timeout, made from a
  dashboard request or a background task the service owns (`service.ai_tasks`, cancelled on stop).
  The monitor, optimizer, execution, burner and closing recorders never wait for one. At most two
  Google calls run at a time across all features.
- **Failures.** A failed call shows on the dashboard (Ask panel status, Decisions > AI line) and
  raises one alert per feature, ET day and message ("AI questions unavailable: ...", "Day-end
  report AI unavailable: ...", "Event calendar AI unavailable: ..."). The text is
  always one of the fixed messages: "Google AI request failed (HTTP n)", "Google AI did not return
  a completed answer", "Invalid Google AI response rejected", "Google AI unavailable; deterministic
  monitoring continues" or "Google AI timed out"; never provider text, never the key.
- **Privacy.** No account ID, broker credential, raw broker string, position, order, fill or quote
  is sent to Google (allowlists below).
- **Official close.** For this contest the IBKR API closing NAV is the official scoring close
  (operator instruction, Oct 21). The supervisor prompt and the dashboard say so; nothing advises a
  statement import.

## "Why isn't it trading?" Ask the app

Overview, directly under Today: readiness. Type a question (at most 500 characters) and press
**Ask**. The answer is labelled **[AI · advice only]** or **[From the readiness checklist]**, with
"Based on:" (the state items it used) and, when useful, "See:" a dashboard place or button. The
last three questions and answers are listed below it. The panel cannot press buttons or change
anything.

**With AI on**, the question goes to Google with an allowlist of the app's current state:

| Item | Content |
|---|---|
| mode, ready, connected, profile | as on the dashboard |
| health | the status line, cleaned |
| checklist | each Today row: label, ✓/!/✗, detail (cleaned; the Account holdings detail is replaced by "Non-contest positions must be closed in TWS (listed on the dashboard)"; leg instructions after "Enter in TWS (Paper):" are cut) |
| continuity | waiting/resume flags, waiting and pause reasons (cleaned) |
| autostart | Auto paper mode, source and pending/active flags |
| burner | configured/effective mode, override, state, reason, window start, stock-entry cutoff, needed, median gap, cycles today |
| alerts | last 24 h, at most 10; app-written alert families are cleaned, any other message (for example a raw broker error) becomes "Broker or other message (see Settings & alerts)" |
| decisions | the latest 5: time, status, eligibility, improvement, threshold, NAV gap, hold/proposed distance, scope, up to 5 blocking reasons (cleaned) |
| risk | passed, up to 10 reasons (cleaned), numeric metrics |
| session_clock, scores | session timing; scoring rule, rate, closing target, NAV estimate, provisional score, previous-close provenance, official-API-close flag |
| day_report, event_caution | the latest day-end report's date, miss, score, status and three largest lines; the approved event caution's session, factors, burner-off flag, status (in effect/expired) and whether a newer proposal awaits approval |
| guide | the operator guide sections "The control bar", "Today: readiness", "Every scored day" and "AI operator features" (at most 8,000 characters) |

"Cleaned" means account IDs become `[account]`, anything after a known raw-broker marker (for
example "Waiting for broker/data:", "the last attempt stopped at:", "Enter in TWS (Paper):", and
the execution and burner reasons that end in IBKR's credit-preview warning or an exception, such as
"Credit/risk preflight blocked:", "Burn credit preview blocked:" or "ended RESIDUAL:") becomes
"[details on the dashboard]"; a burner in ERROR sends only "Closing burner step failed:", control characters are removed and the text is shortened.
Never sent: the account, positions, orders, fills, quotes, contract labels, market-data errors,
unavailable instruments, the last monitor error, setup data, mandated legs, decision narratives,
and the raw text of any alert, Today row or health message. The question itself is cleaned the
same way.

The reply must be JSON with `answer` (at most 1,500 characters), `relied_on` (1-12 state keys) and
`point_to` (one of a fixed list of dashboard places, or "none"); any other field is rejected.
`relied_on` entries that are not keys of the sent state are dropped; if none remain the answer is
still shown with "The AI did not cite the app's state; treat with care."

**Deterministic answer** (AI off, failed, timed out, or 60 AI calls in the last hour): first "The
market is closed" (or "not open yet", or the final minutes before the close) when nothing can trade
now; then every ✗ row, in every mode; then, in paper mode, the latest decision and its first three
blocking reasons only if it is from the last 5 minutes of this session (otherwise "no hedge
decision in the last 5 minutes"); while waiting for data, the waiting reason; otherwise the
Trading row. Then up to three ! rows and the health line, unless a row already says it.

**Limits.** One question every 10 seconds (HTTP 429 "Please wait n s"), one at a time (HTTP 409),
at most 60 AI calls per rolling hour (then deterministic answers with a note). The provider
timeout is 20 s, inside the dashboard's 30 s request timeout. `POST /api/ask {"question"}` needs
the dashboard's control token like every other control.

**Records.** Each answer is stored as an `ai_question` record (`id, at, question, answer,
relied_on, point_to, source, model, status, note, advisory_only`); the last three reload after a
restart.

## Day-end miss explanation

Overview, under the Official scoring ledger. About 16:26 ET on each scored day (after the 16:20
post-close NAV observation and its 5-minute window) the app writes a report for that day by itself
and raises one info alert, "Day-end report for {date} ready: miss ..., score ...". **Recompute now**
rebuilds the selected day (one press per 30 s): use it after late commission reports arrive.
A corrected close, a fill correction or new burner results make a new report automatically. The
date list keeps the last 10 days; the newest report per day is shown.

**The numbers (always shown, AI or not)** come only from what the app recorded: the official
closes (for this contest the IBKR API closing NAV; a manual entry overrides it), the close marks
recorded at the bell and with the post-close NAV observation (16:20-16:25 ET by default, after SPY
options' 16:15 close, so the marks match the official post-close NAV), the day's fills and commissions, and the burner's cycle
results. Each line is in dollars, rounded to cents:

| Line | Meaning |
|---|---|
| Stock and delta moves | Stocks: quantity × change in mark. Options: quantity × multiplier × previous-close delta × change in the underlying |
| Gamma (curvature) | ½ × previous-close gamma × (underlying change)² |
| Implied-volatility moves | previous-close vega × change in implied vol |
| Time decay | previous-close theta × calendar days (3 on a Monday) |
| Option repricing beyond the Greeks | the rest of each option's quote-midpoint change (large moves land here; so does every option without vol/Greeks) |
| IBKR marks vs. quote midpoints | IBKR's own position marks (what the NAV uses) minus the Moomoo midpoints, change between the two closes |
| Hedge trades / Manual TWS trades (fill to close) | each fill marked to today's close: quantity × multiplier × (close mark − fill price) |
| Commissions (hedge and manual) | reported commissions; pending ones are estimated and the report says so |
| Closing burner (fills and fees) | the burner's fills marked to the close, plus their commissions |
| Cash interest (modeled) | the app's cash/borrow/financing model, not IBKR's accrual |
| Unexplained / marking residual | official NAV change minus all lines above, so everything adds up exactly |
| Official NAV change (total), − Benchmark growth, Miss | miss = official close − target; score = \|miss\|, the same as the ledger |

The marks are IBKR's position marks where IBKR marked the contract at every close at which it was
held, otherwise the quote midpoints. The Greeks are taken at the previous close. A per-symbol table
splits the same lines by underlying.

**Reading "Unexplained".** It collects what the app cannot see: marks between the 16:00 bell and
the 16:20 official capture, IBKR's marks for contracts it marked differently from the quotes,
interest and fees the model does not know, and anything held outside the contest list. A large
value (more than $500 and more than a quarter of the NAV change) is flagged. When the previous
close's marks are missing (for example Day 1, if the app was not running through Day 0's
16:00-16:25 window) the Greek lines show "—" and market moves are in Unexplained; when today's
marks are missing, trades too. The report is then labelled "partial data" with a note.

**Items worth checking** (deterministic, always shown): large unexplained residual; IBKR mark
outside the bid/ask (by more than $0.05) at either close; wide (spread over 25% of mid) or
non-live close quote; more than $200 of repricing beyond the Greeks on one contract; holdings that
changed without matching fills; commissions estimated; a score more than 3 × the last forecast +
$100.

**With AI on**, the newest day's report is explained once automatically; **Explain with AI** asks
again for the selected day (one per 60 s, 20 per day). Google receives only the finished report:
the date, official closes, target, miss and score, the driver and per-symbol amounts, the checks,
items worth checking and notes (cleaned like the Q&A text), per-contract labels with today's
quote, marks and implied volatilities only (no per-contract P&L and no previous-close marks, which
together would give each quantity back), the decision counts and most common blocking reasons, burner totals, and the fixed
settings menu. Never sent: the account, positions or quantities, fills, order references,
execution IDs or the manual close's provenance text. The reply must echo the date and contains a
plain-language explanation (at most 2,000 characters), up to 6 items to check and up to 3
suggestions chosen only from: No change, Burner off, Shadow burner, Allow configured paper burner,
Auto paper ON, Auto paper OFF, Profile STANDARD, Review the event caution before the open. Any
other setting, extra field or wrong date is rejected. Suggestions are shown as "Suggested for
tomorrow (not applied)" with no button; nothing is ever applied. A failed attempt shows "AI
explanation unavailable: {fixed message}" and the numbers stay.

**Records and export.** `day_report` (deterministic; id from the date, its inputs and the
commissions, so a restart never duplicates it) and `day_report_ai` (one per AI attempt, ok or
failed). **Export day reports ↗** (`GET /api/export/day_reports`) has one row per date, scope
(Portfolio or symbol) and line, plus one AI row per date with the explanation and suggestions;
text starting with = + - @ is prefixed with ' against spreadsheet formulas.
`POST /api/day-report {"date"?, "action": "compute"|"explain"}` needs the control token: 409
while the official close (or the scoring target) is missing, when AI is off for "explain", or
while an explanation runs; 429 within the rate limits; 422 for a date that is not a scored session.

## Event-risk calendar and event caution

Overview, panel **Event risk today** (under the risk envelope and latest hedge decision).

**The calendar (AI, advice).** On contest session days, between 08:45 and 15:30 ET, the app asks
Google once for a calendar of the session being forecast (the next scored close; on Day 0 that is
Day 1). If the call fails it retries 10 minutes later, at most 3 automatic attempts per day; after
one good calendar it stops for the day. **Refresh event calendar** asks again (at most one call
per 10 minutes and 6 per ET day, automatic ones included; the count survives a restart). With AI
off nothing is fetched and the button is disabled. The call uses **Google Search grounding**: the
model searches the web for scheduled or announced events from now until the next session's open
+ 15 minutes (the horizons the session's forecasts price: the close, overnight and the next
close) that could move SPCX, RKLB, SPY or the market: earnings, FOMC decisions and minutes, CPI,
PPI, payrolls, GDP, rocket launches, major company news, index events, large expirations.

**What is sent:** only `session`, `window_start`, `window_end`, `allowed_symbols` (the mandated
underlyings plus `MARKET`) and `today_et`. Never the account, NAV, positions, orders or app state.

**Strict validation** (pydantic, extra fields rejected): each event has symbols (from the allowed
list only), a type from a fixed list, a title, an ET date, timing (before the open, intraday with an
HH:MM time, after the close, or unknown), impact (low, medium, high), a one-sentence reason and up
to 5 source URLs. Medium and high impact need at least one source; every URL must be a plain
`https://` link with a dotted host, no user name or password and no spaces. An intraday event
needs its time. The reply must echo the session. Events outside the window are dropped (and
counted), by date and time of day: on the first day an event already over (its time has passed, or
"before the open" once the market has opened) is dropped; on the window's last day only events
before the open or with a time at or before the window end are kept (after-close, unknown-time and
later intraday events that day are dropped, so they never drive the current session's caution).
Duplicates are removed. Each source is marked as confirmed when one of Google's own citation
links backs it; others show "(not confirmed by a Google citation)" and still count. Anything else
(another session, a missing source, broken JSON) rejects the whole reply: "Invalid Google AI
response rejected", one alert per day.

**The proposal (deterministic).** The app maps the validated events to ONE setting per symbol:

| Event | Proposed caution |
|---|---|
| high impact on a symbol | that symbol ×1.5 |
| medium impact on a symbol | that symbol ×1.25 |
| low impact | nothing (×1.0) |
| MARKET (economy-wide) | SPY at the event's level; SPCX and RKLB one level lower (high → ×1.25, medium → ×1.0) |

The largest value per symbol wins. **Burner off for the session** is proposed for a high-impact
event on the session day that is an FOMC decision, or intraday at or after 13:00 ET, and concerns
the market or a burner symbol (the 15:40-15:56 burner's median-gap forecast is then unreliable).
The proposal pre-fills the form; the badge shows **APPROVAL PENDING** until you decide.

**Approval (the operator).** Choose ×1.0/×1.25/×1.5 per symbol and the burner box, press **Approve
caution** and confirm. **Clear caution** sets everything back to ×1.0 and counts as your decision
on the calendar shown: its proposal is no longer marked APPROVAL PENDING (a newer calendar is). Only the session being
forecast now can be approved (`POST /api/event-caution {session, factors, burner_off,
calendar_id}`, control token, serialized with the other controls; any other level is rejected).
The approval is a durable `event_caution` record with the factors, the proposal, the calendar's
events as evidence, the symbols each factor scales and `expires_at` (that session's 16:00 close).
It applies at once: the pending hedge plan is dropped and the optimizer and burner re-plan. It
never changes the control epoch, the waiting-for-data state, the profile, the mode or the
configuration hash, so automatic resume after a data wait still works. It **expires by itself**: a
forecast always targets the next close, so after the session's close the next builds use the next
session's approval (none until you approve one). It survives a restart.

**What a caution changes (formula).** For every forecast scenario k and each symbol s with an
approved factor f_s ∈ {1.25, 1.5}, the spot-return shock and the implied-vol shock are scaled:
r̃(k,s) = f_s · r(k,s), ṽ(k,s) = f_s · v(k,s). That widens the closing, next-close and overnight
forecast distributions the optimizer minimizes for that symbol only; correlations are unchanged.
**SPY also scales every broad index ETF in the universe** (IVV, VOO, ...; the configuration's
`BROAD_INDEX_ETFS` list), so an index-proxy hedge moves with the SPY options it hedges. It does
**not** change the stress grid that enforces the 3% limit (fixed spot/vol/skew/rate shocks and the
±5% market rows never read the forecast shocks), so the cap, its feasibility and every hard check
are unchanged. Forecast-based numbers change as intended: hedge proposals, the CVaR metric and
the burner's median gap. The scenario source says "event caution RKLB x1.50 ..."; the scenario
identifier includes the factors only when there are some (builds without a caution keep their
identifier). Decision records and the surplus export carry `event_caution`. A decision built on a
forecast from before an approval changed is discarded and re-planned; the burner waits for a
rebuilt forecast.

**Burner off for the session.** While approved, the closing burner opens no new round trip that
session ("Burner off for this session (approved event caution)"). It only lowers the mode, leaves
the Burner off/Shadow/Paper switch untouched, and an open round trip still sells back what it
bought. The next session uses the configured mode again.

**Google Search display terms.** Grounded results are shown only to the operator, with Google's
own Search Suggestions: the panel embeds the stored snippet from `GET /ai/search-suggestions/{id}`
in a sandboxed frame (no scripts; its own policy `default-src 'none'; style-src 'unsafe-inline';
img-src https: data:; frame-ancestors 'self'`). Source links open unmodified in a new tab, without
a referrer or tracking. Calendars are kept as `event_calendar` records for the contest only.

**Assumption and cost.** The request uses `tools: [{"type": "google_search"}]` together with the
JSON response format; Google documents this combination for Gemini 3 models (checked 2026-10-21).
It is isolated in `event_risk.grounded_tools()` and `supervisor.extract_grounding()`. If Google
refuses the combination, the panel shows "Google AI request failed (HTTP 400)"; there is no silent
fallback. Each search query the model runs is billed after 5,000 free per month; the limits above
keep it to a few calls per session day. Timeout 90 s (95 s overall), 6,144 output tokens.

**Records.** `event_calendar` (random id): session, window, start and fetch times, trigger, model,
status (ok/failed), events, dropped and duplicate counts, notes, citations, search queries, the
Search Suggestions HTML (at most 20 KB), proposal, token usage, fixed error text. `event_caution`
(random id): see above. The newest approval per session wins.

## For maintainers

- `Supervisor.structured(schema, *, instructions, payload, tools=(), max_output_tokens, timeout)`
  in `hedge/supervisor.py` is the one Google call. It refuses with "AI disabled" unless AI is
  enabled (checked again once a slot is free; each feature re-checks after the reply), inlines the pydantic schema (`inline_schema`; Google does not document `$defs`, and
  pydantic still validates everything client side), sends `store: false` and `background: false`,
  appends `ADVISORY`, and maps every error to the fixed messages above. `extract_grounding(body)`
  is the only reader of Google Search citations, queries and suggestions (used by the event
  calendar). The 15-minute review, the Q&A and the day-end explanation use `tools: []`.
- `hedge/ai_common.py`: `operator_text`, `safe_alerts` (with `APP_ALERT_PREFIXES` and
  `RAW_MARKERS`), `RateLimit` and `AIFeature` (status and the once-per-day failure alert).
  **A new app alert family is invisible to the AI until its prefix is added to
  `APP_ALERT_PREFIXES`**; a new raw-text source needs its marker in `RAW_MARKERS`.
- `service.spawn_ai(coroutine, name)` starts tracked background AI work; `stop()` cancels it.
  `service._ai_operator_loop()` runs every 30 s, calls `day_reports.tick(now)` and
  `event_risk.tick(now)` (each synchronous, each in its own try, never awaiting Google) and never
  raises. `ai_common.ai_error(exc)` maps any failure to the fixed texts.
- `hedge/event_risk.py`: `calendar_models(keys)` (the strict reply schema per contest),
  `checked_events`, `propose`, and `EventRisk` (schedule, `refresh()` under its own lock,
  `approve()`, `state()`). The trading path reads only two total hooks over copy-on-write
  in-memory dicts: `ScenarioEngine.event_caution = event_risk.factors_for` (forecast close date ->
  {symbol: factor}) and `ClosingBurner.event_off = event_risk.burner_off`. `event_risk.matches(set)`
  guards `optimize()` (both invalidation checks) and the burner's forecast-freshness check. The
  scale is applied in `ScenarioEngine.build()` between the coverage check and the first reprice;
  `stress_matrix` and the market stress rows never see it.
- `hedge/day_report.py`: `attribute_day(...)` is the pure attribution; `DayReporter` schedules
  (`settle(day)` = close + post-close delay + 5 min + 1 min), records, serves (`latest()`, `view()`,
  `export_rows()`) and explains (`explain()` under its own lock). Close marks are found by
  `CloseMarkRecorder`'s deterministic ids.
- `state()` adds `ai_features`, `qa_history`, `day_reports` and `event_risk` from in-memory caches
  only (no extra database reads on the 2-second poll). The Q&A also sees the latest day report's
  date, miss, score, status and three largest lines (`day_report` state key) and the approved
  caution's session, factors, burner flag, status and whether a newer proposal awaits approval
  (`event_caution`; never the event text or sources). The day-end report records the session's
  approved caution in its context.
