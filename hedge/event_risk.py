"""Event-risk calendar and operator-approved event caution (docs/AI_OPERATOR_FEATURES.md).

Each session morning (about 08:45 ET) and on demand, Google with Google Search grounding lists scheduled
or announced events for the mandated symbols and the market until the next session's open. The reply is
validated strictly and mapped, deterministically, to ONE proposed forecast caution per symbol in
{1.0, 1.25, 1.5} plus an optional closing-burner downgrade for that session. Nothing applies until the
operator presses Approve (control-token endpoint). An approval scales only that session's FORECAST spot
and implied-vol shocks (ScenarioEngine.event_caution), never the stress grid or the 3% cap, never changes
the control epoch, data wait, profile, mode or configuration, and expires at that session's close."""

import asyncio
import math
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as clock
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ai_common import CONTROL, AIFeature, ai_error
from .config import BROAD_INDEX_ETFS, NY
from .models import digest
from .scenarios import OVERNIGHT_REACTION

Day = date  # a field named "date" would shadow the type inside its own annotation
MARKET = "MARKET"
LEVELS = (1.0, 1.25, 1.5)
IMPACT_LEVEL = {"low": 1.0, "medium": 1.25, "high": 1.5}
EVENT_TYPES = (
    "earnings",
    "fomc",
    "cpi",
    "jobs",
    "other_macro",
    "launch",
    "company_news",
    "index_event",
    "options_expiry",
    "other",
)
TIMINGS = ("pre_open", "intraday", "after_close", "unknown")
TIME_ET = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Latest possible time of day (ET) of an event without its own time_et (intraday always has one).
LATEST = {"pre_open": clock(9, 30), "after_close": clock(23, 59), "unknown": clock(23, 59)}
BURNER_EVENT_TIME = "13:00"  # a high-impact event at or after this time makes the 15:40 burner unreliable
MORNING, AFTERNOON = clock(8, 45), clock(15, 30)  # automatic fetch window (ET, session days)
AUTO_ATTEMPTS, SPACING, PER_DAY = 3, timedelta(minutes=10), 6  # Google Search is billed per query
CALENDAR_LIMIT = 20_000  # bytes of stored Search Suggestions HTML
EVENTS_PROMPT = (
    "Build an event-risk calendar for a paper options-hedging contest. Use Google Search to find scheduled "
    "or already-announced events between `window_start` and `window_end` (US Eastern time) that could move "
    "the listed symbols or the US stock market: company earnings (date, and whether before the open or "
    "after the close); FOMC decisions or minutes and Fed-chair remarks; CPI, PPI, payrolls and other jobs "
    "data, and GDP; rocket launches or launch-window changes; major company news; index additions or "
    "rebalances; large options expirations. Include only events a search result supports, with its URL in "
    '`source_urls`. Never guess a date or time; use timing "unknown" when it is not stated. Use symbols '
    "from `allowed_symbols`, and MARKET for economy-wide events. Impact: high means likely to move the "
    "symbol or market well beyond a normal day (earnings, an FOMC decision, CPI, payrolls, a launch "
    "outcome); medium means notable but smaller; low means minor or uncertain. Give `reason` as one plain "
    "sentence. Return at most 20 events and none outside the window. Echo `session`. The operator reviews "
    "this list and alone decides whether to approve a bounded forecast caution."
)


def grounded_tools():
    """The Google Search grounding tool of the interactions API (verified 2026-10-21 against
    ai.google.dev/gemini-api/docs/google-search). Isolated here: if Google refuses tools together with
    a JSON response_format, the call fails visibly ("Google AI request failed (HTTP 400)")."""
    return [{"type": "google_search"}]


def caution_keys(manifest):
    """One caution setting per mandated underlying (SPCX, RKLB, SPY for the contest)."""
    return sorted({manifest.contract_map[i].symbol for i in manifest.mandatory})


def expansions(manifest):
    """Caution key -> scenario symbols it scales. An index key (SPY) also scales every broad index ETF in
    the universe (IVV, VOO, ...): an index proxy hedge must move with the SPY options it hedges."""
    universe = {c.symbol for c in manifest.contracts}
    return {
        key: sorted(
            {key} | ({s for s in universe if s in BROAD_INDEX_ETFS} if key in BROAD_INDEX_ETFS else set())
        )
        for key in caution_keys(manifest)
    }


def https_url(value):
    """https only, dotted host, no userinfo, no whitespace or control characters, at most 500 characters."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or CONTROL.search(value)
        or any(c.isspace() for c in value)
    ):
        raise ValueError("Source URLs must be plain https links")
    parts = urlsplit(value)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or "." not in host.strip(".")
        or host.startswith(".")
        or host.endswith(".")
    ):
        raise ValueError("Source URLs must be plain https links")
    return value


@lru_cache(maxsize=8)
def calendar_models(keys):
    """(CalendarEvent, EventCalendarReply) for one contest: `symbols` is a Literal of its caution keys
    plus MARKET; every other field is fixed. Extra fields are rejected throughout."""
    symbol = Literal[(*keys, MARKET)]

    class CalendarEvent(BaseModel):
        model_config = ConfigDict(extra="forbid")
        symbols: list[symbol] = Field(min_length=1, max_length=4)
        type: Literal[EVENT_TYPES]
        title: str = Field(min_length=3, max_length=160)
        date: Day  # US Eastern calendar date
        timing: Literal[TIMINGS]
        time_et: str | None = None  # "HH:MM" 24 h; required when intraday
        impact: Literal["low", "medium", "high"]
        reason: str = Field(min_length=3, max_length=300)
        source_urls: list[str] = Field(default_factory=list, max_length=5)

        @field_validator("time_et")
        @classmethod
        def clock_time(cls, value):
            if value is not None and not TIME_ET.match(value):
                raise ValueError("time_et must be HH:MM (24 h, ET)")
            return value

        @field_validator("source_urls")
        @classmethod
        def links(cls, value):
            return [https_url(u) for u in value]

        @model_validator(mode="after")
        def consistent(self):
            if self.timing == "intraday" and self.time_et is None:
                raise ValueError("An intraday event needs its time")
            if self.impact in ("medium", "high") and not self.source_urls:
                raise ValueError("A medium or high impact event needs a source URL")
            return self

    class EventCalendarReply(BaseModel):
        model_config = ConfigDict(extra="forbid")
        session: Day  # must equal the requested session
        events: list[CalendarEvent] = Field(max_length=20)
        notes: str = Field(default="", max_length=400)

    return CalendarEvent, EventCalendarReply


def _clean(text):
    return " ".join(CONTROL.sub(" ", str(text)).split())


def in_window(event, start, end):
    """Whether an event can fall inside [start, end] (ET), by its date and time of day: on the first
    date an event known to be over (its time, or pre-open once the market has opened) is out; on the
    last date only one known to come by `end` (pre-open, or its time at or before end) is in, so a
    14:00 FOMC or after-close earnings the day after never drive the current session's caution."""
    first, last = start.astimezone(NY), end.astimezone(NY)
    if not first.date() <= event.date <= last.date():
        return False
    latest = clock.fromisoformat(event.time_et) if event.time_et else LATEST[event.timing]
    if event.date == first.date() and latest < first.time().replace(second=0, microsecond=0):
        return False
    return event.date != last.date() or latest <= last.time()


def checked_events(reply, session, start, end, citations, keys):
    """Post-validation: the reply must echo the session; events outside the window (in_window) are
    dropped (counted), duplicates removed, and each URL flagged `cited` when a Google url_citation backs it."""
    if reply.session != session:
        raise ValueError("Invalid Google AI response rejected")  # a calendar for another day
    cited = {c["url"]: c.get("title") or "" for c in citations}
    order = {k: i for i, k in enumerate((*keys, MARKET))}
    events, seen, dropped, duplicates = [], set(), 0, 0
    for event in reply.events:
        if not in_window(event, start, end):
            dropped += 1
            continue
        symbols = sorted(set(event.symbols), key=order.get)
        key = (tuple(symbols), event.type, event.date, _clean(event.title).lower())
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        events.append(
            {
                "symbols": symbols,
                "type": event.type,
                "title": _clean(event.title),
                "date": event.date.isoformat(),
                "timing": event.timing,
                "time_et": event.time_et,
                "impact": event.impact,
                "reason": _clean(event.reason),
                "sources": [
                    {"url": u, "title": _clean(cited.get(u, ""))[:200], "cited": u in cited}
                    for u in dict.fromkeys(event.source_urls)
                ],
            }
        )
    timing = {t: i for i, t in enumerate(("pre_open", "intraday", "after_close", "unknown"))}
    events.sort(
        key=lambda e: (e["date"], timing[e["timing"]], e["time_et"] or "", -IMPACT_LEVEL[e["impact"]])
    )
    return events, dropped, duplicates


def propose(events, session, keys, burner_symbols):
    """Deterministic proposal: each event's symbols at IMPACT_LEVEL[impact]; MARKET at full level for index
    keys (SPY) and one level lower for the others; the maximum per key (default 1.0). Burner off when a
    high-impact market/burner-symbol event on the session is an FOMC decision or intraday at or after
    13:00 ET (the 15:40-15:56 burner's median-gap forecast is then unreliable). Low impact never proposes."""
    factors = {k: 1.0 for k in keys}
    reasons = {k: [] for k in keys}
    burner_reason = []
    for event in events:
        level = IMPACT_LEVEL[event["impact"]]
        targets = {}
        for symbol in event["symbols"]:
            if symbol == MARKET:
                for key in keys:
                    lowered = LEVELS[max(0, LEVELS.index(level) - 1)]
                    targets[key] = max(targets.get(key, 1.0), level if key in BROAD_INDEX_ETFS else lowered)
            elif symbol in factors:
                targets[symbol] = max(targets.get(symbol, 1.0), level)
        for key, value in targets.items():
            if value > 1.0:
                factors[key] = max(factors[key], value)
                reasons[key].append(event["title"])
        late = event["type"] == "fomc" or (
            event["timing"] == "intraday" and (event.get("time_et") or "") >= BURNER_EVENT_TIME
        )
        if (
            event["impact"] == "high"
            and event["date"] == str(session)
            and late
            and (MARKET in event["symbols"] or set(event["symbols"]) & set(burner_symbols))
        ):
            burner_reason.append(event["title"])
    return {
        "factors": factors,
        "burner_off": bool(burner_reason),
        "reasons": {k: v for k, v in reasons.items() if v},
        "burner_reason": burner_reason,
    }


def _not_before(later, earlier):
    """ISO instants: later >= earlier (False when either is missing or unreadable)."""
    try:
        return datetime.fromisoformat(str(later)) >= datetime.fromisoformat(str(earlier))
    except (TypeError, ValueError):
        return False


def caution_text(factors, burner_off=None):
    text = ", ".join(f"{k} ×{float(f):.2f}" for k, f in factors.items())
    if burner_off is None:
        return text
    return text + ("; burner off for the session" if burner_off else "; burner unchanged")


class EventsBusy(ValueError):  # HTTP 409
    pass


class EventsLimit(ValueError):  # HTTP 429
    pass


class EventRisk(AIFeature):
    """Calendar fetch (AI, background task), deterministic proposal, operator approvals and the two
    total lookup hooks the trading path reads: factors_for(day) (scenario engine) and burner_off(day)
    (closing burner). The hooks read immutable snapshots of in-memory dicts and never raise."""

    name, label = "events", "Event calendar AI"

    def __init__(self, service):
        super().__init__(service)
        self.last_status = "Not fetched yet"
        self.calendars = {}  # id -> payload; insertion order = age (newest last)
        self.attempts = []  # (started_at, trigger) of every Google call: automatic schedule + ET-day cap
        self._approved = {}  # session date -> newest approval; replaced (copy-on-write), never mutated
        self.last_approval = None
        self.epoch = 0  # bumps on every approval (audit/debug)
        self.keys = caution_keys(service.manifest)
        self.expands = expansions(service.manifest)

    @property
    def manifest(self):
        return self.service.manifest

    # ------------------------------------------------------------------ records
    def load(self):
        account, contest = self.manifest.account, self.manifest.contest_id
        store = self.service.store
        calendars = [c for c in store.records("event_calendar", account, 20) if c.get("contest") == contest]
        self.calendars = {c["id"]: c for c in reversed(calendars)}
        self.attempts = [
            (datetime.fromisoformat(c["started_at"]), c.get("trigger"))
            for c in reversed(calendars)
            if c.get("started_at")
        ]
        approvals = [a for a in store.records("event_caution", account, 50) if a.get("contest") == contest]
        approved, ordered = {}, []
        for a in approvals:
            try:
                at, session = datetime.fromisoformat(a["approved_at"]), date.fromisoformat(a["session"])
                ordered.append((at, a.get("seq") or 0, session, self._prepare(a)))
            except (KeyError, TypeError, ValueError):
                continue
        ordered.sort(key=lambda x: x[:2])  # oldest first: the newest approval per session wins
        for _, _, session, a in ordered:
            approved[session] = a
        self._approved = approved
        self.last_approval = ordered[-1][3] if ordered else None

    @staticmethod
    def _prepare(record):
        return {**record, "_expires": datetime.fromisoformat(record["expires_at"])}

    # ------------------------------------------------------------------ trading-path hooks (total)
    def factors_for(self, day):
        """Scenario symbol -> factor for builds whose forecast close is `day`; only factors != 1.0."""
        try:
            approval = self._approved.get(day)
            if not approval:
                return {}
            result = {}
            for key, factor in approval["factors"].items():
                if float(factor) in (1.25, 1.5):
                    for symbol in self.expands.get(key, (key,)):
                        result[symbol] = max(result.get(symbol, 1.0), float(factor))
            return result
        except Exception:
            return {}

    def burner_off(self, day):
        """True while the approved caution for session `day` turned the closing burner off (until its close)."""
        try:
            approval = self._approved.get(day)
            return bool(approval and approval.get("burner_off") and self.service.now() < approval["_expires"])
        except Exception:
            return False

    def matches(self, scenarios):
        """The set carries exactly the caution currently approved for the close it forecasts (so a set
        built before an approval changed never becomes a plan). Fails open on a malformed set."""
        try:
            at = getattr(scenarios, "built_at", None) or self.service.now()
            end = self.manifest.next_close(at)
            symbols = {self.manifest.contract_map[i].symbol for i in scenarios.ids}
            expected = (
                {s: f for s, f in self.factors_for(end.astimezone(NY).date()).items() if s in symbols}
                if end
                else {}
            )
            return dict(getattr(scenarios, "event_caution", None) or {}) == expected
        except Exception:
            return True

    def approval_for(self, day):
        """The approval that applied to session `day` (for the day-end report context), or None."""
        a = self._approved.get(day)
        if not a:
            return None
        return {k: a.get(k) for k in ("factors", "burner_off", "approved_at", "calendar_id")}

    # ------------------------------------------------------------------ session and window
    def session_for(self, now):
        end = self.manifest.next_close(now)
        return end.astimezone(NY).date() if end else None

    def window(self, session, now):
        """now -> the next scored session's open + 15 min (the horizons the session's forecasts price:
        closing, overnight, next close); session close + 1 day for the last session."""
        m = self.manifest
        following = m.next_scored_session(session)
        end = (
            m.session_open(following) + OVERNIGHT_REACTION
            if following
            else m.session_close(session) + timedelta(days=1)
        )
        return now, end

    # ------------------------------------------------------------------ calendar fetch
    def _limit(self, now):
        """(seconds to wait, message) under the shared limits: 1 call per 10 min, 6 per ET day."""
        today = now.astimezone(NY).date()
        count = sum(at.astimezone(NY).date() == today for at, _ in self.attempts)
        if count >= PER_DAY:
            return 3600.0, f"Event calendar limit ({PER_DAY} per day) reached; it resets tomorrow"
        if self.attempts:
            wait = (self.attempts[-1][0] + SPACING - now).total_seconds()
            if wait > 0:
                return wait, f"Please wait {math.ceil(wait / 60)} min before refreshing the event calendar"
        return 0.0, ""

    def tick(self, now):
        """Every 30 s from the service's AI loop: on contest session days between 08:45 and 15:30 ET,
        one automatic calendar per day for the session being forecast (up to 3 attempts, 10 min apart).
        Synchronous and cheap; the Google call runs in a service-owned background task."""
        s, m = self.service, self.manifest
        if not s.supervisor.enabled or self.lock.locked():
            return
        et = now.astimezone(NY)
        today = et.date()
        if today not in (m.day_zero, *m.scoring_sessions) or not MORNING <= et.time() < AFTERNOON:
            return
        session = self.session_for(now)
        if session is None:
            return
        if any(
            c.get("status") == "ok"
            and c.get("session") == str(session)
            and datetime.fromisoformat(c["started_at"]).astimezone(NY).date() == today
            for c in self.calendars.values()
            if c.get("started_at")
        ):
            return
        automatic = [
            at
            for at, trigger in self.attempts
            if trigger == "automatic" and at.astimezone(NY).date() == today
        ]
        if len(automatic) >= AUTO_ATTEMPTS or self._limit(now)[0] > 0:
            return
        self._start(now, session, "automatic")

    def start_refresh(self, trigger="operator"):
        """Dashboard POST /api/events/refresh: 409 when AI is off or a fetch runs, 429 within the limits."""
        s = self.service
        now = s.now()
        if not s.supervisor.enabled:
            raise EventsBusy("Turn on AI (Decisions > AI) to fetch the event calendar")
        if self.lock.locked():
            raise EventsBusy("Still fetching the event calendar")
        session = self.session_for(now)
        if session is None:
            raise EventsBusy("The contest has no future scored close")
        wait, message = self._limit(now)
        if wait > 0:
            raise EventsLimit(message)
        self._start(now, session, trigger)
        return {"status": "started", "session": str(session)}

    def _start(self, now, session, trigger):
        self.attempts.append((now, trigger))
        self.service.spawn_ai(self.refresh(trigger, now, session), f"event-calendar-{session}")

    async def refresh(self, trigger, now, session):
        """One grounded Google call; records an event_calendar (ok with events and proposal, or failed)."""
        async with self.lock:
            s, m = self.service, self.manifest
            start, end = self.window(session, now)
            payload = {
                "session": str(session),
                "window_start": start.astimezone(NY).isoformat(timespec="minutes"),
                "window_end": end.astimezone(NY).isoformat(timespec="minutes"),
                "allowed_symbols": [*self.keys, MARKET],
                "today_et": now.astimezone(NY).date().isoformat(),
            }
            record = {
                "contest": m.contest_id,
                "session": str(session),
                "window_start": payload["window_start"],
                "window_end": payload["window_end"],
                "started_at": now.isoformat(),
                "trigger": trigger,
                "status": "failed",
                "model": None,
                "events": [],
                "dropped": 0,
                "duplicates": 0,
                "notes": "",
                "citations": [],
                "search_queries": [],
                "search_suggestions_html": "",
                "proposal": None,
                "usage": None,
                "error": None,
            }
            try:
                _, reply_model = calendar_models(tuple(self.keys))
                reply = await asyncio.wait_for(
                    s.supervisor.structured(
                        reply_model,
                        instructions=EVENTS_PROMPT,
                        payload=payload,
                        tools=grounded_tools(),
                        max_output_tokens=6144,
                        timeout=90,
                    ),
                    95,
                )
                if not s.supervisor.enabled:  # turned off while Google answered: the reply is dropped
                    raise ValueError("AI disabled")
                events, dropped, duplicates = checked_events(
                    reply.output, session, start, end, reply.citations, self.keys
                )
                proposal = propose(events, session, self.keys, m.burner.symbols)
                record.update(
                    status="ok",
                    model=reply.model,
                    events=events,
                    dropped=dropped,
                    duplicates=duplicates,
                    notes=_clean(reply.output.notes),
                    citations=list(reply.citations),
                    search_queries=list(reply.search_queries),
                    search_suggestions_html=reply.search_suggestions[:CALENDAR_LIMIT],
                    proposal=proposal,
                    usage=reply.usage,
                )
                self.ok(
                    f"Calendar for {session}: {len(events)} event{'s' if len(events) != 1 else ''}"
                    + (f" ({dropped} outside the window dropped)" if dropped else "")
                    + "; advice only, nothing applies until you approve"
                )
            except Exception as exc:  # CancelledError (shutdown) still propagates
                record["error"] = ai_error(exc)
                if record["error"] == "AI disabled":  # the operator turned AI off: no proposal, no alert
                    self.ok(
                        "AI turned off: calendar reply discarded; the approved caution (if any) is unchanged"
                    )
                else:
                    self.fail(record["error"], "The approved event caution (if any) is unchanged")
            record["fetched_at"] = s.now().isoformat()
            identifier = uuid4().hex
            try:
                s.store.record("event_calendar", m.account, record, identifier)
            except Exception:
                pass  # shown from memory until the next restart
            self.calendars[identifier] = {"id": identifier, **record}
            proposal = record["proposal"]
            if proposal and (any(f > 1.0 for f in proposal["factors"].values()) or proposal["burner_off"]):
                try:
                    s.alert(
                        "info",
                        f"Event calendar for {session} proposes a forecast caution: "
                        f"{caution_text(proposal['factors'], proposal['burner_off'])}. Review it under Event "
                        "risk today on Overview; nothing applies until you press Approve caution",
                        key=("event_calendar", str(session), digest(proposal)[:16]),
                    )
                except Exception:
                    pass
            return self.calendars[identifier]

    # ------------------------------------------------------------------ approval
    def approve(self, session, factors, burner_off=False, calendar_id=None):
        """Operator approval (dashboard, control token, under control_lock) for the session being
        forecast now. Durable record first, then the in-memory switch; wakes the monitor and burner."""
        s, m = self.service, self.manifest
        now = s.now()
        current = self.session_for(now)
        if current is None:
            raise ValueError("The contest has no future scored close")
        if session != current:
            raise ValueError(
                f"Caution can only be approved for the session being forecast now ({current}); "
                "it expires at that session's close"
            )
        unknown = sorted(set(factors) - set(self.keys))
        if unknown:
            raise ValueError(
                f"Unknown caution symbols: {', '.join(unknown)} (allowed: {', '.join(self.keys)})"
            )
        if any(float(v) not in LEVELS for v in factors.values()):
            raise ValueError("Caution factors must be 1.0, 1.25 or 1.5")
        calendar = None
        if calendar_id is not None:
            calendar = self.calendars.get(calendar_id)
            if calendar is None or calendar.get("session") != str(session) or calendar.get("status") != "ok":
                raise ValueError("That event calendar is not a valid calendar for this session")
        full = {k: float(factors.get(k, 1.0)) for k in self.keys}
        record = {
            "contest": m.contest_id,
            "session": str(session),
            "factors": full,
            "burner_off": bool(burner_off),
            "proposed": calendar.get("proposal") if calendar else None,
            "calendar_id": calendar_id,
            "calendar_fetched_at": calendar.get("fetched_at") if calendar else None,
            "evidence": calendar.get("events") if calendar else [],
            "expands": {k: self.expands.get(k, [k]) for k in self.keys},
            "approved_at": now.isoformat(),
            "expires_at": m.scoring_close(session).astimezone(NY).isoformat(),
            "config_hash": m.config_hash,  # informational only: never used to invalidate anything
            "seq": time.time_ns(),
        }
        identifier = s.store.record("event_caution", m.account, record)
        prepared = self._prepare({"id": identifier, **record})
        self._approved = {**self._approved, session: prepared}  # copy-on-write: builds read snapshots
        self.last_approval = prepared
        self.epoch += 1
        # Re-plan against the new forecast set; never touches control_epoch, data_wait, profile or mode.
        # The plan goes with its recovery context (as on a data wait): a leftover would keep the surplus
        # add-on "busy" and make a recovery-mode change pause paper. execution.recovery_checks stays: it
        # still governs a recovery order working at the broker.
        s.pending_target = s.pending_recovery = None
        s.active_plan_started = False
        s.force_optimize = True
        s.monitor_wakeup.set()
        s.burner.wake()
        effect = (
            caution_text(full, bool(burner_off))
            if any(f > 1.0 for f in full.values()) or burner_off
            else "cleared (all 1.0; burner unchanged)"
        )
        try:
            s.alert(
                "info", f"Event caution approved for {session}: {effect}", key=("event_caution", identifier)
            )
        except Exception:
            pass
        return self.state()

    # ------------------------------------------------------------------ dashboard
    def _approval_view(self, approval, current):
        now = self.service.now()
        status = (
            "in effect"
            if approval is current and now < approval["_expires"]
            else "expired"
            if now >= approval["_expires"]
            else "approved"
        )
        return {
            **{
                k: approval.get(k)
                for k in (
                    "id",
                    "session",
                    "factors",
                    "burner_off",
                    "approved_at",
                    "expires_at",
                    "calendar_id",
                )
            },
            "status": status,
        }

    def state(self):
        """For the 2-second poll, from memory only."""
        s = self.service
        now = s.now()
        session = self.session_for(now)
        mine = [c for c in self.calendars.values() if session and c.get("session") == str(session)]
        # The newest good calendar stays on screen after a failed refresh (shown as latest_failure).
        calendar = next((c for c in reversed(mine) if c.get("status") == "ok"), mine[-1] if mine else None)
        newest = mine[-1] if mine else None
        failure = (
            {"fetched_at": newest.get("fetched_at"), "error": newest.get("error")}
            if newest is not None and newest is not calendar and newest.get("status") != "ok"
            else None
        )
        current = self._approved.get(session) if session else None
        shown = current or self.last_approval
        approval = self._approval_view(shown, current) if shown else None
        proposal = (calendar or {}).get("proposal") if (calendar or {}).get("status") == "ok" else None
        approved = (
            (current["factors"], bool(current.get("burner_off")))
            if current
            else ({k: 1.0 for k in self.keys}, False)
        )
        # The operator decided on this calendar when the session's approval (or Clear) names it or
        # came after it was fetched: its proposal is no longer pending, whatever was chosen.
        decided = bool(current and calendar) and (
            current.get("calendar_id") == calendar.get("id")
            or _not_before(current.get("approved_at"), calendar.get("fetched_at"))
        )
        pending = bool(
            proposal
            and not decided
            and (
                {k: float(proposal["factors"].get(k, 1.0)) for k in self.keys} != approved[0]
                or bool(proposal["burner_off"]) != approved[1]
            )
        )
        today = now.astimezone(NY).date()
        return {
            "session": str(session) if session else None,
            "symbols": list(self.keys),
            "levels": list(LEVELS),
            "expands": self.expands,
            "status": self.status(),
            "calendar": {
                **{
                    k: calendar.get(k)
                    for k in (
                        "id",
                        "session",
                        "fetched_at",
                        "trigger",
                        "status",
                        "model",
                        "events",
                        "dropped",
                        "duplicates",
                        "notes",
                        "proposal",
                        "error",
                        "window_start",
                        "window_end",
                    )
                },
                "has_suggestions": bool(calendar.get("search_suggestions_html")),
            }
            if calendar
            else None,
            "approval": approval,
            "in_effect": dict(getattr(s.last_scenarios, "event_caution", None) or {}),
            "pending_approval": pending,
            "latest_failure": failure,
            "calls_today": sum(at.astimezone(NY).date() == today for at, _ in self.attempts),
            "calls_per_day": PER_DAY,
        }

    def suggestions_html(self, calendar_id):
        """Stored Google Search Suggestions snippet for GET /ai/search-suggestions/{id} (None if absent)."""
        calendar = self.calendars.get(calendar_id)
        if calendar is None:
            calendar = self.service.store.get_record(calendar_id, self.manifest.account, "event_calendar")
        html = (calendar or {}).get("search_suggestions_html")
        return html if isinstance(html, str) and html.strip() else None
