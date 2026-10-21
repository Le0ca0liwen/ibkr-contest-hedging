"""'Why isn't it trading?' Q&A (docs/AI_OPERATOR_FEATURES.md). Advice only: answers come from the
app's own current state and operator-guide excerpts; it reads state and writes one ai_question
record, nothing else. Without AI (off, failed, rate-limited) the readiness checklist answers."""

from __future__ import annotations

import asyncio
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .ai_common import AIFeature, RateLimit, ai_error, operator_text, safe_alerts
from .config import NY
from .day_report import top_drivers

POINT_TO = (
    "none",
    "Today: readiness",
    "Enable paper trading",
    "Pause trading",
    "Reconcile",
    "Start shadow",
    "Auto paper",
    "Confirm contest configuration",
    "Refresh price history now",
    "Closing surplus burner",
    "Import an official closing NAV",
    "Alerts & required actions",
    "Event risk today",  # panel titles exactly as the dashboard shows them
    "Day-end miss explanation",
    "TWS (Paper)",
    "Moomoo OpenD",
)
GUIDE_SECTIONS = (
    "The control bar (top of every page)",
    "Today: readiness (top of Overview)",
    "Every scored day (Oct 22-23, Oct 26-30, Nov 2-4)",
    "AI operator features",
)
GUIDE_LIMIT = 8000  # the four sections whole (about 6,600 characters with the event-risk text)
GUIDE_PATH = Path(__file__).parents[1] / "docs/CONTEST_OPERATOR_GUIDE.md"
DECISION_FIELDS = (
    "at",
    "status",
    "trade_eligible",
    "corrective",
    "improvement",
    "threshold",
    "nav_gap",
    "hold_distance",
    "proposed_distance",
    "optimization_scope",
)
UNCITED = "The AI did not cite the app's state; treat with care."
RECENT_DECISION = timedelta(minutes=5)  # an older decision does not explain why nothing trades now
QA_PROMPT = (
    "You answer an operator's question about a paper options-hedging contest app. Use ONLY the JSON "
    "input: `state` (the app's current status) and `guide` (excerpts of the operator guide). Do not use "
    "outside knowledge about markets, brokers or this app. If the input does not answer the question, "
    "say so and name the dashboard place to check. Write plain language for a non-specialist, at most "
    "120 words, no formulas. Use button names exactly as the guide writes them. Put in `relied_on` only "
    "keys from `state_keys` that you actually used, and in `point_to` the one dashboard place or button "
    'that matters most (or "none"). Never promise that trading will start, stop or succeed: the app\'s '
    "deterministic checks decide."
)


class QAAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str = Field(min_length=1, max_length=1500)
    relied_on: list[Annotated[str, Field(max_length=80)]] = Field(min_length=1, max_length=12)
    point_to: Literal[POINT_TO] = "none"  # a dashboard place/button, shown as text only


class QALimit(ValueError):  # HTTP 429
    pass


class QABusy(ValueError):  # HTTP 409
    pass


def guide_excerpt(path=GUIDE_PATH):
    """Allowlisted '## ' sections of the operator guide, in order, at most GUIDE_LIMIT characters."""
    try:
        text = Path(path).read_text()
    except OSError:
        return ""
    sections = re.split(r"(?m)^## ", text)[1:]
    kept = [s.strip() for s in sections if s.split("\n", 1)[0].strip() in GUIDE_SECTIONS]
    return "\n\n".join("## " + s for s in kept)[:GUIDE_LIMIT]


def _number(value):
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _sentence(text):
    text = " ".join(str(text).split())
    return text if not text or text.endswith((".", "!", "?", "…")) else text + "."


def _parsed(value):
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at if at.tzinfo else None


def _et(value):
    try:
        return datetime.fromisoformat(str(value)).astimezone(NY).strftime("%b %d %H:%M ET")
    except ValueError:
        return "time unknown"


def _day_report(reports):
    """Latest day-end report as numbers and app-written driver labels only (no contracts or notes)."""
    report = (reports or [None])[0]
    if not isinstance(report, dict):
        return None
    official = report.get("official") or {}
    return {
        "date": report.get("date"),
        "miss": _number(_float(official.get("miss"))),
        "score": _number(_float(official.get("score"))),
        "status": report.get("status"),
        "top_drivers": top_drivers(report),
    }


def _event_caution(risk):
    """The approved event caution as symbols, levels and flags only (no event text or sources)."""
    approval = (risk or {}).get("approval")
    if not isinstance(approval, dict):
        return None
    return {
        "session": approval.get("session"),
        "factors": {
            str(k)[:12]: v for k, v in (approval.get("factors") or {}).items() if _number(v) is not None
        },
        "burner_off": bool(approval.get("burner_off")),
        "status": approval.get("status")
        if approval.get("status") in ("in effect", "expired", "approved")
        else None,
        "pending_approval": bool((risk or {}).get("pending_approval")),
    }


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class OperatorQA(AIFeature):
    name, label = "qa", "AI questions"

    def __init__(self, service):
        super().__init__(service)
        self.interval = RateLimit(min_interval=10)
        self.hourly = RateLimit(per_hour=60)
        self.recent = []
        self.guide = guide_excerpt()

    def load(self):
        self.recent = self.service.store.records("ai_question", self.service.manifest.account, 3)

    def history(self):
        return list(self.recent)

    def context(self, state):
        """Allowlist of the app's state for Google: no account, positions, orders, fills, quotes,
        raw alert/checklist/health text or broker strings (section 4.3)."""
        account = self.service.manifest.account

        def text(value, limit=240):
            return operator_text(value, account, limit)

        checklist = []
        for row in state.get("checklist") or []:
            label = text(row.get("label"), 60)
            detail = (
                "Non-contest positions must be closed in TWS (listed on the dashboard)"
                if row.get("label") == "Account holdings"
                else text(row.get("detail"))
            )
            status = row.get("status") if row.get("status") in ("ok", "warn", "fail") else "warn"
            checklist.append({"label": label, "status": status, "detail": detail})
        continuity = state.get("continuity") or {}
        wait = continuity.get("wait") or {}
        autostart = state.get("autostart") or {}
        burner = state.get("burner") or {}
        window = burner.get("window") or {}
        risk = state.get("risk")
        scores = state.get("scores") or {}
        safe = {
            "mode": state.get("mode"),
            "ready": state.get("ready"),
            "connected": state.get("connected"),
            "profile": state.get("profile"),
            "health": text(state.get("health")),
            "checklist": checklist,
            "continuity": {
                "automatic_resume_pending": continuity.get("automatic_resume_pending"),
                "resume_mode": wait.get("resume_mode"),
                "autostart": wait.get("autostart"),
                "reason": text(wait.get("reason")) or None,
                "pause_reason": text(continuity.get("pause_reason")) or None,
            },
            "autostart": {k: autostart.get(k) for k in ("mode", "configured", "source", "pending", "active")},
            "burner": {
                "configured_mode": burner.get("configured_mode"),
                "effective_mode": burner.get("effective_mode"),
                "override": burner.get("override"),
                "event_off": burner.get("event_off"),
                "state": burner.get("state"),
                # ERROR holds a bare "{exception type}: {text}" (service._burner_loop): never sent.
                "reason": text(
                    "Closing burner step failed: " if burner.get("state") == "ERROR" else burner.get("reason")
                ),
                "window_start": window.get("start"),
                "stock_entry_cutoff": window.get("stock_entry_cutoff"),
                "needed": _number(burner.get("needed")),
                "gap_median": _number((burner.get("gap") or {}).get("median")),
                "cycles_today": (burner.get("today") or {}).get("cycles"),
            },
            "alerts": safe_alerts(state.get("alerts"), account, self.service.now()),
            "decisions": [
                {
                    **{k: d.get(k) for k in DECISION_FIELDS},
                    "blocking_reasons": [text(r) for r in (d.get("blocking_reasons") or [])[:5]],
                }
                for d in (state.get("decisions") or [])[:5]
            ],
            "risk": {
                "passed": risk.get("passed"),
                "reasons": [text(r) for r in (risk.get("reasons") or [])[:10]],
                "metrics": {
                    str(k)[:40]: v for k, v in (risk.get("metrics") or {}).items() if _number(v) is not None
                },
            }
            if risk
            else None,
            "session_clock": state.get("session_clock"),
            "scores": {
                "scoring_rule": scores.get("scoring_rule"),
                "annual_rate": scores.get("annual_rate"),
                "closing_target": state.get("closing_target"),
                "nav_estimate": state.get("nav_estimate"),
                "provisional_score": state.get("provisional_score"),
                "previous_close_provenance": state.get("previous_close_provenance"),
                "api_close_official": state.get("api_close_official"),
            },
            "day_report": _day_report(state.get("day_reports")),
            "event_caution": _event_caution(state.get("event_risk")),
        }
        keys = [
            "mode",
            "ready",
            "connected",
            "profile",
            "health",
            *(f"checklist.{row['label']}" for row in checklist),
            *(k for k in safe if k not in ("mode", "ready", "connected", "profile", "health", "checklist")),
            "guide",
        ]
        return {
            "now_et": self.service.now().astimezone(NY).isoformat(timespec="minutes"),
            "state_keys": keys,
            "state": safe,
            "guide": self.guide,
        }

    def _market(self, clock):
        """A plain sentence when nothing can trade now (session_clock), else None."""
        if not isinstance(clock, dict) or clock.get("options_tradable") or clock.get("stocks_tradable"):
            return None
        if not clock.get("session"):
            return "The market is closed: there is no contest session today."
        left = _number(clock.get("minutes_to_close"))
        if left is None or left <= 0:
            return "The market is closed for today: hedge orders resume after the next session opens."
        try:
            opened = self.service.now() >= self.service.manifest.session_open(
                date.fromisoformat(clock["session"])
            )
        except (TypeError, ValueError, KeyError):
            opened = True
        return (
            "Hedging has stopped for the final minutes before the close."
            if opened
            else "The market is not open yet: hedge orders start after the open."
        )

    def fallback(self, state, note=None):
        """Deterministic answer from the local state (never sent anywhere, so full row details): market
        hours first, then every ✗ checklist row (in every mode), then the mode's own reason (a decision
        only from the last few minutes of this session), up to three ! rows, then health if not yet said."""
        rows = [r for r in state.get("checklist") or [] if isinstance(r, dict)]
        mode, health = state.get("mode"), state.get("health")
        decisions = state.get("decisions") or []
        used, relied, parts = [], ["mode"], []
        fails = [r for r in rows if r.get("status") == "fail"]
        market = self._market(state.get("session_clock"))
        if market:
            parts.append(market)
            relied.append("session_clock")
        if fails:
            listed = "; ".join(f"{r['label']}: {r['detail']}" for r in fails)
            parts.append(
                f"Paper trading is on, but these checks fail: {listed}"
                if mode == "PAPER"
                else f"Paper trading is blocked by: {listed}"
            )
            used += fails
        if mode == "PAPER":
            now = self.service.now()
            latest = decisions[0] if decisions else None
            at = _parsed((latest or {}).get("at"))
            recent = (
                at is not None
                and timedelta(0) <= now - at <= RECENT_DECISION
                and at.astimezone(NY).date() == now.astimezone(NY).date()
            )
            if recent and not market:
                if not latest.get("trade_eligible"):
                    reasons = "; ".join(str(r) for r in (latest.get("blocking_reasons") or [])[:3])
                    parts.append(
                        f"The latest decision ({_et(latest.get('at'))}) did not trade: "
                        f"{reasons or 'no qualifying trade'}"
                    )
                else:
                    parts.append("The latest decision was eligible; orders follow the execution checks")
                relied.append("decisions")
            elif not fails:
                parts.append(
                    "Paper trading is on"
                    if market
                    else "Paper trading is on; no hedge decision has been recorded yet"
                    if latest is None
                    else f"Paper trading is on; no hedge decision in the last 5 minutes (the last one: "
                    f"{_et(latest.get('at'))})"
                )
        elif mode == "WAITING_DATA":
            if not fails:
                wait = (state.get("continuity") or {}).get("wait") or {}
                parts.append(
                    f"Paper trading waits for its checks: {wait.get('reason') or 'data or broker state'}. "
                    "It resumes by itself once they pass"
                )
                relied.append("continuity")
        elif not fails:
            trading = next((r for r in rows if r.get("label") == "Trading"), None)
            parts.append(trading["detail"] if trading else f"{str(mode).title()}: {health}")
            used += [trading] if trading else []
        warns = [r for r in rows if r.get("status") == "warn" and r not in used][:3]
        parts += [f"Also check: {r['label']}: {r['detail']}" for r in warns]
        relied += [f"checklist.{r['label']}" for r in (*used, *warns)]
        answer = " ".join(_sentence(x) for x in parts)
        if health and " ".join(str(health).split()) not in answer:
            answer = " ".join(filter(None, (answer, _sentence(f"Health: {health}"))))
        if health:
            relied.append("health")
        return {
            "answer": answer[:2000],
            "relied_on": relied,
            "point_to": "Today: readiness",
            "source": "deterministic",
            "model": None,
            "note": note,
        }

    async def ask(self, question):
        if self.lock.locked():
            raise QABusy("Still answering the previous question")
        wait = self.interval.check()
        if wait:
            raise QALimit(f"Please wait {math.ceil(wait)} s before asking again")
        self.interval.hit()
        async with self.lock:
            service = self.service
            supervisor = service.supervisor
            state = service.state()
            if not supervisor.enabled:
                result = self.fallback(state, note="AI is off: answer from the readiness checklist")
                self.ok("AI off: answered from the readiness checklist")
            elif self.hourly.check():
                result = self.fallback(
                    state,
                    note="AI question limit (60 per hour) reached; answer from the readiness checklist",
                )
                self.ok("AI question limit reached: answered from the readiness checklist")
            else:
                self.hourly.hit()
                try:
                    payload = self.context(state)
                    reply = await asyncio.wait_for(
                        supervisor.structured(
                            QAAnswer,
                            instructions=QA_PROMPT,
                            payload={
                                "question": operator_text(question, service.manifest.account, 500),
                                **payload,
                            },
                            max_output_tokens=2048,
                            timeout=20,
                        ),
                        25,
                    )
                    if not supervisor.enabled:  # turned off while Google answered: the reply is dropped
                        raise ValueError("AI disabled")
                    keys = set(payload["state_keys"])
                    relied = list(dict.fromkeys(k for k in reply.output.relied_on if k in keys))
                    result = {
                        "answer": reply.output.answer,
                        "relied_on": relied,
                        "point_to": reply.output.point_to,
                        "source": "ai",
                        "model": reply.model,
                        "note": None if relied else UNCITED,
                    }
                    self.ok("Answered; advice only")
                except Exception as exc:  # CancelledError (shutdown) still propagates
                    message = ai_error(exc)  # fixed texts only, never provider output
                    if message == "AI disabled":  # the operator turned AI off meanwhile: no alert
                        self.ok("AI off: answered from the readiness checklist")
                        note = "AI is off: answer from the readiness checklist"
                    else:
                        self.fail(message, "Answers come from the readiness checklist")
                        note = f"AI unavailable ({message}); answer from the readiness checklist"
                    result = self.fallback(state, note=note)
            result = {
                "id": uuid4().hex,
                "at": service.now().isoformat(),
                "question": question,
                **result,
                "status": self.last_status,
                "advisory_only": True,
            }
            try:
                service.store.record("ai_question", service.manifest.account, result, result["id"])
            except Exception:
                result["note"] = " ".join(
                    filter(None, (result["note"], "(Not saved: database unavailable.)"))
                )
            self.recent = [result, *self.recent][:3]
            return result
