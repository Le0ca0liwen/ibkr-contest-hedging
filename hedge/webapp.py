from __future__ import annotations

import csv
import io
import math
import secrets
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai_qa import QABusy, QALimit
from .day_report import DayReportBusy, DayReportLimit, DayReportWaiting
from .event_risk import LEVELS, EventsBusy, EventsLimit
from .execution_quality import execution_quality


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    profile: str | None = None


class CloseImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    nav: Decimal
    source: str
    account: str

    @field_validator("nav", mode="before")
    @classmethod
    def plain_number(cls, value):
        # Accept the statement figure as printed, e.g. "$1,002,345.67".
        return value.replace("$", "").replace(",", "").strip() if isinstance(value, str) else value


class ConfigConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_hash: str


class AISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    enabled: bool


class RecoverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str


class BurnerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "shadow", "paper"]


class AutostartSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "paper"]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=500)


SessionDate = date  # a field named "date" with a default would shadow the type in its annotation


class DayReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: SessionDate | None = None  # default: the latest scored session whose close has passed
    action: Literal["compute", "explain"] = "compute"


class CautionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: date
    factors: dict[str, Annotated[float, Field(strict=True)]] = Field(max_length=8)
    burner_off: bool = False
    calendar_id: str | None = Field(default=None, max_length=64)

    @field_validator("factors")
    @classmethod
    def levels(cls, value):
        if any(v not in LEVELS for v in value.values()):  # only these levels; nothing else applies
            raise ValueError("Caution factors must be 1.0, 1.25 or 1.5")
        return value


class EventsRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Google Search Suggestions (grounding display terms): Google's own snippet, framed only by this
# dashboard, no scripts (and the iframe is sandboxed without allow-scripts), links open unmodified.
SUGGESTIONS_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'"
)
SUGGESTIONS_PATH = "/ai/search-suggestions/"


class DailyRateImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    daily_rate: Decimal
    source: str


def json_safe(value):
    """NaN/Infinity as text: Python's JSON parser accepts them in a request, but a 422 body that
    echoes them cannot be encoded (FastAPI's default handler then fails with HTTP 500)."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def create_app(service):
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="Contest Hedge", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_control(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-control-token", ""), token):
                return Response("Local control token required", status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return Response("Cross-origin control rejected", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith(SUGGESTIONS_PATH):
            response.headers["Content-Security-Policy"] = SUGGESTIONS_CSP
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": json_safe(jsonable_encoder(exc.errors()))}, status_code=422)

    assets = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=assets), name="static")

    @app.post("/api/ai/settings")
    async def ai_settings(body: AISettings):
        try:
            result = service.supervisor.configure(body.model, body.enabled)
            service.store.record("supervisor_settings", service.manifest.account, result)
            return result
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/ask")
    async def ask(body: Question):
        # Advice only: reads the app's state (plus Google when AI is on) and records the answer.
        try:
            return await service.qa.ask(body.question)
        except QALimit as exc:  # too soon after the previous question
            raise HTTPException(429, str(exc)) from None
        except QABusy as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/day-report")
    async def day_report(body: DayReportRequest):
        # Deterministic attribution of a scored day's miss; 'explain' starts optional AI text in the
        # background. Advice only: neither changes any trading state.
        try:
            return service.day_reports.request(body.date, body.action)
        except DayReportLimit as exc:
            raise HTTPException(429, str(exc)) from None
        except (DayReportWaiting, DayReportBusy) as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/events/refresh")
    async def events_refresh(body: EventsRefresh):
        # Starts the grounded Google calendar fetch in the background; advice only (proposes, never applies).
        try:
            return service.event_risk.start_refresh("operator")
        except EventsLimit as exc:
            raise HTTPException(429, str(exc)) from None
        except EventsBusy as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/event-caution")
    async def event_caution(body: CautionApproval):
        # The operator's approval: bounded forecast caution for the session being forecast now, plus an
        # optional burner-off for that session. Serialized with controls; never bumps control_epoch.
        try:
            async with service.control_lock:
                return service.event_risk.approve(
                    body.session, body.factors, body.burner_off, body.calendar_id
                )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get(SUGGESTIONS_PATH + "{calendar_id}", response_class=HTMLResponse)
    async def search_suggestions(calendar_id: str):
        html = service.event_risk.suggestions_html(calendar_id) if len(calendar_id) <= 64 else None
        if html is None:
            raise HTTPException(404, "No search suggestions for that calendar")
        return HTMLResponse(
            '<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            f'<base target="_blank"></head><body style="margin:0">{html}</body></html>'
        )

    @app.post("/api/daily-cash-rate")
    async def daily_cash_rate(body: DailyRateImport):
        try:
            result = service.ledger.import_daily_rate(body.date, body.daily_rate, body.source)
            service.pending_target = None
            service.force_optimize = True
            return result
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/", response_class=HTMLResponse)
    async def index():
        page = (assets / "index.html").read_text().replace("__CONTROL_TOKEN__", token)
        if service.manifest.scoring_rule == "pending_interest_adjustment":
            page = page.replace("Today's closing target", "Reported input; revised target pending")
            page = page.replace(
                "Score = sum of absolute changes between consecutive official closing NAVs. Day 0 is the baseline.",
                "Interest-adjusted scoring formula pending. Preserve raw IBKR closing NAV; official scores remain unknown.",
            )
        elif service.manifest.scoring_rule in (
            "interest_benchmark",
            "calendar_interest_benchmark",
            "actual_daily_cash_rate",
        ):
            page = page.replace("Today's closing target", "Raw previous close · adjusted target below")
            page = page.replace(
                "Score = sum of absolute changes between consecutive official closing NAVs. Day 0 is the baseline.",
                "Score = sum |NAV today − NAV previous × (1 + daily cash rate)|. Day 0 is the baseline. Actual per-day rates are required; fixed 3.35% is not used."
                if service.manifest.scoring_rule == "actual_daily_cash_rate"
                else "Score = sum |NAV today − NAV previous × (1 + r/360)^n|, n = calendar days since the previous session. Day 0 is the baseline."
                if service.manifest.scoring_rule == "calendar_interest_benchmark"
                else "Score = sum |NAV today − NAV previous × (1 + r/360)|. Day 0 is the baseline.",
            )
        return page

    @app.post("/api/recovery")
    async def recovery_settings(body: RecoverySettings):
        try:
            async with service.control_lock:
                if service.pending_recovery is not None:
                    await service.execution.pause()
                    service.pending_target = service.pending_recovery = None
                    service.execution.recovery_checks = None
                    service.mode = "SHADOW"
                return service.recovery.set_mode(body.mode)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/burner")
    async def burner_settings(body: BurnerSettings):
        # Downgrade-only switch for NEW burner entries; an open cycle still restores its inventory.
        # It never changes the control epoch, so automatic data-wait resume is unaffected.
        try:
            async with service.control_lock:
                return service.burner.set_mode(body.mode)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/autostart")
    async def autostart_settings(body: AutostartSettings):
        # Persisted switch: 'paper' starts paper trading automatically once every activation
        # check passes (now from PAUSED or SHADOW, and after each launch); 'off' cancels a pending start.
        # Not under control_lock: 'off' must take effect while an automatic activation holds the
        # lock, and its epoch bump makes that activation refuse to enable execution.
        try:
            return service.set_autostart(body.mode)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/confirm-config")
    async def confirm_config(body: ConfigConfirmation):
        try:
            async with service.control_lock:
                return service.confirm_configuration(body.config_hash)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/daily-history/refresh")
    async def refresh_history():
        # Quote-only Moomoo download; never changes the trading mode.
        try:
            return service.start_history_refresh("Dashboard request")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/state")
    async def state():
        return service.state()

    @app.get("/api/addons/surplus-input")
    async def surplus_input():
        try:
            return service.surplus_snapshot()
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/api/control")
    async def control(body: Control):
        try:
            return await service.control(body.action, body.profile)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (TimeoutError, ConnectionError) as exc:
            connected = (
                service.broker.snapshot().connected if service.demo else service.broker.ib.isConnected()
            )
            if service.last_monitor_error and not connected:
                # TWS answered, but the app refused the account (e.g. non-contest positions):
                # restarting TWS would not help, so name the real cause.
                raise HTTPException(
                    409,
                    "Nothing was changed. TWS answers, but the app's last connection attempt stopped "
                    f"at: {service.last_monitor_error}",
                ) from exc
            raise HTTPException(
                503,
                "IBKR did not answer (TWS disconnected or busy); nothing was changed. "
                "Check TWS (Paper) and try again.",
            ) from exc

    @app.post("/api/closes")
    async def import_close(body: CloseImport):
        try:
            if body.date in service.manifest.scoring_sessions or body.date == service.manifest.day_zero:
                if service.now() < service.manifest.scoring_close(body.date):
                    raise ValueError(
                        "That session has not closed yet ("
                        + (
                            "its official close is read after 20:00 ET"
                            if service.manifest.extended_hours
                            else "16:00 ET"
                        )
                        + ")"
                    )
            # Guard against a typo (a missing digit moves every later target and risk limit).
            # Independent references only, so a mistyped earlier entry can still be corrected.
            references = [
                Decimal(str(nav))
                for nav in (
                    service.last_snapshot.nav if service.last_snapshot else None,
                    service.manifest.initial_nav,
                )
                if nav
            ]
            if references and all(abs(body.nav / reference - 1) > Decimal("0.2") for reference in references):
                raise ValueError(
                    f"{body.nav:,.2f} differs by more than 20% from the current and the starting NAV "
                    f"({' / '.join(f'{reference:,.2f}' for reference in references)}); "
                    "check the statement figure"
                )
            result = service.ledger.import_close(body.date, body.nav, body.source, body.account)
            service.force_optimize = True
            service.pending_target = None
            return result
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/export/{category}")
    async def export(category: str):
        account = service.manifest.account
        if category == "scores":
            rows = service.state()["scores"]["days"]
        elif category == "burn_cycles":
            rows = service.burner.export_rows()
        elif category == "orders":
            rows = service.store.intents(account)  # complete history, unlike the bounded state()
        elif category == "fills":
            rows = service.store.fills(account)
        elif category == "execution_quality":
            rows = execution_quality(service.store, account, service.manifest.contract_map)
        elif category == "decisions":
            rows = service.state()["decisions"]
        elif category == "day_reports":
            rows = service.day_reports.export_rows()
        else:
            raise HTTPException(404, "Unknown export")
        buffer = io.StringIO()
        keys = sorted({k for row in rows for k in row})
        writer = csv.DictWriter(buffer, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            # Spreadsheet formula injection protection for broker/AI strings.
            safe = {
                k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                for k, v in row.items()
            }
            writer.writerow(safe)
        return Response(
            buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{category}.csv"'},
        )

    return app
