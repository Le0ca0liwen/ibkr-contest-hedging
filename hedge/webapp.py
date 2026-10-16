from __future__ import annotations

import csv
import io
import secrets
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware


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


class AISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    enabled: bool


class RecoverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str


class DailyRateImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    daily_rate: Decimal
    source: str


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
        return response

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
        elif service.manifest.scoring_rule in ("interest_benchmark", "actual_daily_cash_rate"):
            page = page.replace("Today's closing target", "Raw previous close · adjusted target below")
            page = page.replace(
                "Score = sum of absolute changes between consecutive official closing NAVs. Day 0 is the baseline.",
                "Score = sum |NAV today − NAV previous × (1 + daily cash rate)|. Day 0 is the baseline. Actual per-day rates are required; fixed 3.35% is not used."
                if service.manifest.scoring_rule == "actual_daily_cash_rate"
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
            raise HTTPException(
                503,
                "Broker reconciliation timed out or disconnected; activation blocked. Reconcile before retrying.",
            ) from exc

    @app.post("/api/closes")
    async def import_close(body: CloseImport):
        try:
            result = service.ledger.import_close(body.date, body.nav, body.source, body.account)
            service.force_optimize = True
            service.pending_target = None
            return result
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/export/{category}")
    async def export(category: str):
        state = service.state()
        if category == "scores":
            rows = state["scores"]["days"]
        elif category in ("fills", "orders", "decisions", "execution_quality"):
            rows = state[category]
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
