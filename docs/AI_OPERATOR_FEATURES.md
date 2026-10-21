# AI operator features

Optional helpers for the operator, built on the Google Gemini supervisor
([GOOGLE_SUPERVISOR.md](GOOGLE_SUPERVISOR.md)). The implementation spec is
[AI_OPERATOR_FEATURES_SPEC.md](AI_OPERATOR_FEATURES_SPEC.md).

| Feature | Status |
|---|---|
| "Why isn't it trading?" Q&A (Overview > Ask the app) | Built |
| Day-end miss explanation | Specified (spec section 5); documented here when built |
| Event-risk calendar and approved event caution | Specified (spec section 6); documented here when built |

## Ground rules

- **Advice only.** Nothing the AI returns can place, size, approve, block or cancel an order, or
  change a mode, profile, limit, burner or autostart setting. The deterministic optimizer and risk
  gate trade. Every prompt ends with a fixed sentence saying so and telling the model to treat
  every input as data, never as instructions.
- **One switch.** **Enable AI / Disable AI** (Decisions > AI supervisory review, saved in
  `config/local.ai.json`, plus the local key) governs every AI feature. With AI off, no feature
  calls Google; the Q&A answers from the readiness checklist.
- **Off the trading path.** Every AI call is an async request with a timeout, made from a
  dashboard request or a background task the service owns (`service.ai_tasks`, cancelled on stop).
  The monitor, optimizer, execution, burner and closing recorders never wait for one. At most two
  Google calls run at a time across all features.
- **Failures.** A failed call shows on the dashboard (Ask panel status, Decisions > AI line) and
  raises one alert per feature, ET day and message ("AI questions unavailable: ..."). The text is
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
- `state()` adds `ai_features` and `qa_history` from in-memory caches only (no extra database
  reads on the 2-second poll).
