# AI operator features

Optional helpers for the operator, built on the Google Gemini supervisor
([GOOGLE_SUPERVISOR.md](GOOGLE_SUPERVISOR.md)). The implementation spec is
[AI_OPERATOR_FEATURES_SPEC.md](AI_OPERATOR_FEATURES_SPEC.md).

| Feature | Status |
|---|---|
| "Why isn't it trading?" Q&A (Overview > Ask the app) | Built |
| Day-end miss explanation (Overview, under the Official scoring ledger) | Built |
| Event-risk calendar and approved event caution | Specified (spec section 6); documented here when built |

## Ground rules

- **Advice only.** Nothing the AI returns can place, size, approve, block or cancel an order, or
  change a mode, profile, limit, burner or autostart setting. The deterministic optimizer and risk
  gate trade. Every prompt ends with a fixed sentence saying so and telling the model to treat
  every input as data, never as instructions.
- **One switch.** **Enable AI / Disable AI** (Decisions > AI supervisory review, saved in
  `config/local.ai.json`, plus the local key) governs every AI feature. With AI off, no feature
  calls Google; the Q&A answers from the readiness checklist and the day-end report shows its
  numbers only.
- **Off the trading path.** Every AI call is an async request with a timeout, made from a
  dashboard request or a background task the service owns (`service.ai_tasks`, cancelled on stop).
  The monitor, optimizer, execution, burner and closing recorders never wait for one. At most two
  Google calls run at a time across all features.
- **Failures.** A failed call shows on the dashboard (Ask panel status, Decisions > AI line) and
  raises one alert per feature, ET day and message ("AI questions unavailable: ...", "Day-end
  report AI unavailable: ..."). The text is
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
| guide | the operator guide sections "The control bar", "Today: readiness", "Every scored day" and "AI operator features" (at most 6,000 characters) |

"Cleaned" means account IDs become `[account]`, anything after a known raw-broker marker (for
example "Waiting for broker/data:", "the last attempt stopped at:", "Enter in TWS (Paper):")
becomes "[details on the dashboard]", control characters are removed and the text is shortened.
Never sent: the account, positions, orders, fills, quotes, contract labels, market-data errors,
unavailable instruments, the last monitor error, setup data, mandated legs, decision narratives,
and the raw text of any alert, Today row or health message. The question itself is cleaned the
same way.

The reply must be JSON with `answer` (at most 1,500 characters), `relied_on` (1-12 state keys) and
`point_to` (one of a fixed list of dashboard places, or "none"); any other field is rejected.
`relied_on` entries that are not keys of the sent state are dropped; if none remain the answer is
still shown with "The AI did not cite the app's state; treat with care."

**Deterministic answer** (AI off, failed, timed out, or 60 AI calls in the last hour): in paper
mode, the latest decision and its first three blocking reasons; otherwise every ✗ row; otherwise,
while waiting for data, the waiting reason; otherwise the Trading row. Then up to three ! rows and
the health line.

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
recorded at the bell and after the close, the day's fills and commissions, and the burner's cycle
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
| = Official NAV change, − Benchmark growth, = Miss | miss = official close − target; score = \|miss\|, the same as the ledger |

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
items worth checking and notes (cleaned like the Q&A text), per-contract labels and prices (no
quantities), the decision counts and most common blocking reasons, burner totals, and the fixed
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

## For maintainers

- `Supervisor.structured(schema, *, instructions, payload, tools=(), max_output_tokens, timeout)`
  in `hedge/supervisor.py` is the one Google call. It refuses with "AI disabled" unless AI is
  enabled, inlines the pydantic schema (`inline_schema`; Google does not document `$defs`, and
  pydantic still validates everything client side), sends `store: false` and `background: false`,
  appends `ADVISORY`, and maps every error to the fixed messages above. `extract_grounding(body)`
  is the only reader of Google Search citations, queries and suggestions (used by the event
  calendar). The 15-minute review uses the same call with `tools: []`.
- `hedge/ai_common.py`: `operator_text`, `safe_alerts` (with `APP_ALERT_PREFIXES` and
  `RAW_MARKERS`), `RateLimit` and `AIFeature` (status and the once-per-day failure alert).
  **A new app alert family is invisible to the AI until its prefix is added to
  `APP_ALERT_PREFIXES`**; a new raw-text source needs its marker in `RAW_MARKERS`.
- `service.spawn_ai(coroutine, name)` starts tracked background AI work; `stop()` cancels it.
  `service._ai_operator_loop()` runs every 30 s, calls `day_reports.tick(now)` (deterministic,
  never awaits Google) and never raises. `ai_common.ai_error(exc)` maps any failure to the fixed
  texts.
- `hedge/day_report.py`: `attribute_day(...)` is the pure attribution; `DayReporter` schedules
  (`settle(day)` = close + post-close delay + 5 min + 1 min), records, serves (`latest()`, `view()`,
  `export_rows()`) and explains (`explain()` under its own lock). Close marks are found by
  `CloseMarkRecorder`'s deterministic ids.
- `state()` adds `ai_features`, `qa_history` and `day_reports` from in-memory caches only (no
  extra database reads on the 2-second poll). The Q&A also sees the latest day report's date, miss,
  score, status and three largest lines (`day_report` state key).
