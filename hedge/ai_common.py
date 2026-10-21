"""Shared plumbing for the AI operator features (docs/AI_OPERATOR_FEATURES.md): prompt-safe text,
alert summaries, rolling rate limits and per-feature status with one de-duplicated failure alert.
Nothing here calls Google; Supervisor.structured() does."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from .config import NY

ACCOUNT_ID = re.compile(r"\b(?:DU|DF|DI|U|F)\d{5,9}\b")
RAW_MARKERS = (  # app text that is followed by raw broker/exception text: cut after the marker
    "the last attempt stopped at:",
    "contract verification failed:",
    "Waiting for broker/data:",
    "Close these non-contest positions in TWS:",
    "Enter in TWS (Paper):",
    "Live API market data unavailable:",
    "Durable event processing failed:",
    "Closing burner step failed:",
    "Unsupported actual broker positions",
    "Automatic resume waiting for full preflight:",
    "automatic preflight retry:",
    # Execution/burner reasons (health, burner.reason, checklist rows, RESIDUAL alerts) that end in
    # IBKR what-if warningText or an ib_insync exception.
    "Credit/risk preflight blocked:",
    "Burn credit preview blocked:",
    "Broker credit preview warning:",
    "Fresh execution validation:",
    "Fresh burn validation:",
    "Protected setup preflight:",
    "Protected package submission uncertain; reconcile, never resend:",
    "Broker or data unavailable:",
    "ended RESIDUAL:",
)
APP_ALERT_PREFIXES = (  # app-authored alert families that may be summarized for the AI
    "Closing NAV capture missed",
    "Post-close NAV",
    "Closing NAV",
    "Previous official closing NAV",
    "Moomoo quote collector",
    "Another Moomoo quote collector",
    "Moomoo collector is not subscribed",
    "Moomoo quote list",
    "Daily price history download failed",
    "Live API market data unavailable",
    "Working-order check",
    "Mandatory holdings mismatch",
    "Scheduled mandatory purchase window",
    "Closing burner",
    "Late fill on a closing burner leg",
    "Cannot reach TWS",
    "Retrying IBKR",
    "Automatic",
    "AI ",
    "Day-end report",
    "Event calendar",
    "Event caution",
)
OTHER_ALERT = "Broker or other message (see Settings & alerts)"
DETAILS = " [details on the dashboard]"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
UNAVAILABLE = "Google AI unavailable; deterministic monitoring continues"
FIXED_ERRORS = ("AI disabled", "Invalid Google AI response rejected", "Google AI ")


def ai_error(exc):
    """A failed AI call as one fixed text: Supervisor.structured()'s own ValueErrors pass; a timeout or
    anything else (including app-side errors) is replaced, never echoed."""
    if type(exc) is ValueError and str(exc).startswith(FIXED_ERRORS):
        return str(exc)
    return "Google AI timed out" if isinstance(exc, TimeoutError) else UNAVAILABLE


def operator_text(text, account=None, limit=240):
    """App-authored free text made safe for a prompt: account IDs -> [account]; anything after a
    RAW_MARKERS marker replaced by ' [details on the dashboard]'; control characters removed;
    whitespace collapsed; truncated to `limit`."""
    if text is None:
        return ""
    text = str(text)
    if account:
        text = text.replace(str(account), "[account]")
    text = ACCOUNT_ID.sub("[account]", text)
    cuts = [(i, m) for m in RAW_MARKERS if (i := text.find(m)) >= 0]
    if cuts:
        index, marker = min(cuts)
        text = text[: index + len(marker)] + DETAILS
    text = " ".join(CONTROL.sub(" ", text).split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _at(value):
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def safe_alerts(alerts, account, now, limit=10):
    """Alerts of the last 24 h as {"severity", "at", "text"}. An alert whose message starts with an
    APP_ALERT_PREFIXES entry passes through operator_text; any other message (e.g. a raw broker
    201/202 error text) becomes OTHER_ALERT."""
    cutoff, result = now - timedelta(hours=24), []
    for alert in alerts or []:
        at = _at(alert.get("at"))
        if at is None or at < cutoff:
            continue
        message = str(alert.get("message") or "")
        severity = alert.get("severity")
        result.append(
            {
                "severity": severity if severity in ("info", "warning", "error") else "warning",
                "at": at.isoformat(),
                "text": operator_text(message, account)
                if message.startswith(APP_ALERT_PREFIXES)
                else OTHER_ALERT,
            }
        )
        if len(result) >= limit:
            break
    return result


class RateLimit:
    """Rolling limits on a monotonic clock: RateLimit(min_interval=10, per_hour=60, per_day=None).
    check() -> seconds to wait (0 = allowed); hit() records an attempt. Failures count."""

    def __init__(self, min_interval=0, per_hour=None, per_day=None, clock=None):
        self.min_interval, self.per_hour, self.per_day = min_interval, per_hour, per_day
        self.clock = clock or time.monotonic
        self.hits = deque()

    def check(self):
        now = self.clock()
        horizon = 86400 if self.per_day is not None else 3600 if self.per_hour is not None else 0
        while self.hits and len(self.hits) > 1 and self.hits[0] <= now - horizon:
            self.hits.popleft()
        waits = [0.0]
        if self.hits and self.min_interval:
            waits.append(self.hits[-1] + self.min_interval - now)
        for count, window in ((self.per_hour, 3600), (self.per_day, 86400)):
            recent = [t for t in self.hits if t > now - window]
            if count is not None and len(recent) >= count:
                waits.append(recent[-count] + window - now)
        return max(waits)

    def hit(self):
        self.hits.append(self.clock())


class AIFeature:
    """Status + once-per-day failure alert for one AI feature. Subclasses set name and label."""

    name, label = "feature", "AI feature"

    def __init__(self, service):
        self.service = service
        self.lock = asyncio.Lock()
        self.last_status = "Not used yet"
        self.last_at = None
        self.last_error = None

    def status(self):
        return {
            "enabled": self.service.supervisor.enabled,
            "busy": self.lock.locked(),
            "status": self.last_status,
            "last_at": self.last_at,
            "last_error": self.last_error,
        }

    def ok(self, message):
        self.last_status, self.last_error, self.last_at = message, None, self.service.now().isoformat()

    def fail(self, message, fallback):
        """`message` is one of Supervisor.structured()'s fixed texts, never provider output."""
        self.last_status, self.last_error = f"{message}. {fallback}", message
        self.last_at = self.service.now().isoformat()
        day = self.service.now().astimezone(NY).date().isoformat()
        try:
            self.service.alert(
                "warning",
                f"{self.label} unavailable: {message}. {fallback}",
                key=("ai", self.name, day, message),
            )
        except Exception:
            pass  # a failed alert write must not turn advice into an error
