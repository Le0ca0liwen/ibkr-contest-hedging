# Google Gemini supervisor

Default model: **gemini-3.8-flash**. Dashboard → Hedge decisions includes a model
selector (3.8 Flash / 3.5 Flash-Lite), Apply model, and Enable/Disable AI.
Availability and quota depend on your Google project; no silent model fallback.

Double-click `Setup Google AI.command` to enter a key locally with hidden input.
It writes `.runtime/gemini-api-key` with mode 0600, excluded from Git. You can
also use GEMINI_API_KEY or GOOGLE_API_KEY environment variables. Keys never
appear in browser state, alerts or decision records. Rotate any key pasted into
chat in Google AI Studio; use the same helper to replace it.

The review runs independently of the broker and optimizer every 15 minutes,
including PAUSED operation, after an account snapshot exists. It reads a small
allowlist of status flags, quote counts, numerical risk metrics and recent
decision metrics. It does not receive account IDs, raw broker strings, positions,
statements, broker credentials, or database credentials. These summaries are
sent to Google for inference; Google's applicable data/billing terms still apply.

Requests use structured JSON, no tools, no server-side interaction storage,
a 30-second request timeout and 1,024 output tokens. No overlapping calls or
automatic immediate retries. Rate limiting applies to failed calls too.
Disabling AI stops subsequent calls; an already-running call may finish.

Replies must preserve the input snapshot ID and choose a validated profile.
Currently STANDARD is the only approved profile. Arbitrary positions, limit
changes, extra fields, unvalidated profiles and incomplete replies are rejected.
Recommendations are recorded as advice only: they never apply a profile or
enable trading. Reviews that finish after the account changes are marked stale.
The existing deterministic strategy continues on model failures. The scoring sentence in the
prompt is generated from the manifest's `scoring_rule` and rate; for the SPCX/RKLB/SPY contest it
is previous closing NAV × (1 + 0.0344/360)^n_i. Reviews also receive the NAV estimate, closing
target, provisional score, session clock (minutes to close, n_i, oldest stock/option quote age),
risk reasons and the latest decisions' gap/threshold figures. A review is marked stale only when
the account content changes during the call, not when the snapshot is merely re-stamped.
Official closing inputs and executable market-data validation remain required.

Model changes are local AI settings, not hedge strategy changes. Changes do not
reset the 15-minute cooldown. Selection persists in ignored config/local.ai.json.
To run after restarting the dashboard, keep the local key file or environment
variable available. A successful model call is required to verify your key and
project access; passing mocked tests is not that verification.

Official references checked October 6, 2026:

- https://ai.google.dev/gemini-api/docs/models
- https://ai.google.dev/gemini-api/docs/api-key
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/api/interactions-api
