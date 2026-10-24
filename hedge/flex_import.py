"""Nightly import of the IBKR Activity Statement, the contest's scored close (instructor, Oct 23).

IBKR closes the securities reporting window at about 20:20 ET and publishes statements "around
midnight" (Web API docs). From 00:10 ET after each session the importer asks the Flex Web Service
(hedge/flex.py) for that day's statement only, records its NAV as the day's official close (a
statement row, superseding the app's provisional API row), and keeps the statement's own position
marks to calibrate the predicted statement NAV (hedge/statement.py). Not ready yet: it asks again
every 30 minutes. It never trades and never runs without the operator's Flex token and query
(ops/setup_ibkr_flex.py).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from .config import NY
from .flex import LOCKED_OUT, FlexError, fetch_statement, parse_statement, read_flex_settings
from .models import digest

FIRST_TRY = timedelta(hours=8, minutes=10)  # after the regular close: 00:10 ET
RETRY = timedelta(minutes=30)
FATAL_RETRY = timedelta(hours=6)  # a token or query problem the operator must fix first
LOCKOUT_RETRY = timedelta(hours=1)  # IBKR locked the token (1025): one request an hour, any day
HISTORY = timedelta(days=45)  # paper daily statements cover the last 45 days
SOURCE = "IBKR Flex Activity Statement"
CENT = Decimal("0.01")


def record_id(manifest, day):
    return digest((manifest.contest_id, str(day), "flex_statement"))[:32]


class StatementImporter:
    def __init__(self, service, root=".", fetch=fetch_statement, settings=read_flex_settings):
        self.service, self.root, self.fetch, self.settings = service, Path(root), fetch, settings
        self.attempted = {}  # day -> (when, retry delay)
        self.failures = {}  # day -> consecutive failures
        self.status = "Not set up: double-click Setup IBKR Flex.command"
        self.last = None
        self.locked_until = None  # after a 1025 lockout no day is asked for before this

    @property
    def manifest(self):
        return self.service.manifest

    def configured(self):
        return self.settings(self.root) is not None

    def imported(self, day):
        return self.service.store.get_record(
            record_id(self.manifest, day), self.manifest.account, "flex_statement"
        )

    def due(self, now):
        """Contest days whose statement should exist by now and has not been imported, oldest first."""
        m, days = self.manifest, []
        for day in (m.day_zero, *m.scoring_sessions):
            ready = m.session_close(day) + FIRST_TRY
            if not ready <= now <= ready + HISTORY or self.imported(day):
                continue
            last = self.attempted.get(day)
            if last is None or now >= last[0] + last[1]:
                days.append(day)
        return days

    async def step(self, now):
        """Import at most one due statement. Never raises."""
        credentials = self.settings(self.root)
        if credentials is None:
            self.status = "Not set up: double-click Setup IBKR Flex.command"
            return None
        if self.status.startswith("Not set up"):
            self.status = ""  # set up: the dashboard shows "waiting for the next statement"
        if self.locked_until is not None and now < self.locked_until:
            return None
        due = self.due(now)
        if not due:
            return None
        day = due[0]
        try:
            text = await self.fetch(*credentials, from_date=day, to_date=day)
            result = self.record(day, parse_statement(text), now)
        except FlexError as exc:
            if exc.code == LOCKED_OUT:
                self._locked_out(day, now, str(exc))
                return None
            self._failed(day, now, str(exc), RETRY if exc.retry else FATAL_RETRY, fatal=not exc.retry)
            return None
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            self._failed(day, now, f"{type(exc).__name__}: {exc}", RETRY)
            return None
        self.attempted.pop(day, None)
        self.failures.pop(day, None)
        self.locked_until = None
        return result

    def _locked_out(self, day, now, reason):
        """IBKR locked the token after too many failed requests: every further request may extend
        it, so nothing is asked for an hour, whichever day is due."""
        self.locked_until = now + LOCKOUT_RETRY
        self.attempted[day] = (now, LOCKOUT_RETRY)
        self.status = (
            f"IBKR has locked the Flex token for now ({reason}); asking again hourly, "
            f"next at {self.locked_until.astimezone(NY):%H:%M} ET"
        )
        self.service.alert(
            "warning",
            f"Automatic IBKR statement import paused: {reason}. The app asks again once an hour. "
            "If it still fails in the morning, generate a new token in Client Portal (Performance & "
            "Reports > Flex Queries > Flex Web Service) and run Setup IBKR Flex.command; you can also "
            "enter the statement NAV in Settings & alerts.",
            key=("flex_locked", str(now.date())),
        )

    def _failed(self, day, now, reason, delay, fatal=False):
        self.attempted[day] = (now, delay)
        count = self.failures[day] = self.failures.get(day, 0) + 1
        self.status = f"Statement for {day} not imported yet: {reason}"
        if fatal or count == 4:  # about two hours of "not ready" is worth a look
            self.service.alert(
                "warning",
                f"Automatic IBKR statement import for {day} failed: {reason}. "
                + (
                    "Check the Flex token and query (Setup IBKR Flex.command)"
                    if fatal
                    else "It keeps retrying every 30 minutes"
                )
                + "; you can also enter the statement NAV in Settings & alerts.",
                key=("flex_import", str(day), fatal),
            )

    def record(self, day, statement, now):
        """Store the statement and record its NAV as the day's official close. ValueError (retried)
        when it is not this account's statement for `day` yet."""
        m, service = self.manifest, self.service
        if statement["account"] and statement["account"] != m.account:
            raise FlexError(f"The Flex query reports account {statement['account']}, not {m.account}")
        row = statement["nav"].get(day)
        total = row and row.get("total")
        if total is None and statement.get("ending_date") == day:
            total, row = statement["ending_value"], row or {}
        if total is None or total <= 0:
            raise ValueError(f"No NAV for {day} in the statement yet (statement period {statement['to']})")
        total = Decimal(total).quantize(CENT)
        contracts = m.contract_map
        positions = {
            str(p["conid"]): {
                "label": contracts[p["conid"]].label if p["conid"] in contracts else p["symbol"],
                "quantity": str(p["quantity"]),
                "mark": str(p["mark"]),
                "value": str(p["value"]),
            }
            for p in statement["positions"]
            if p["report_date"] in (None, day) and p["quantity"] is not None and p["mark"] is not None
        }
        before = service.ledger.close_records().get(day)
        payload = {
            "contest": m.contest_id,
            "date": str(day),
            "account": statement["account"] or m.account,
            "nav": {k: (str(v) if v is not None else None) for k, v in (row or {}).items()},
            "total": str(total),
            "positions": positions,
            "fetched_at": now.isoformat(),
            "provisional_before": str(Decimal(str(before["nav"])).quantize(CENT)) if before else None,
            "provisional_source": before["source"] if before else None,
        }
        accrued = row.get("interest_accruals") if row else None
        api = self.api_accrued(day)
        payload["accrual_gap"] = (
            str(Decimal(accrued) - api) if accrued is not None and api is not None else None
        )
        payload["comparison"] = service.statement_marks_comparison(
            day, {k: {"quantity": v["quantity"], "close": v["mark"]} for k, v in positions.items()}
        )
        service.store.record("flex_statement", m.account, payload, record_id(m, day))
        parts = ", ".join(
            f"{name} {Decimal(row[key]):,.2f}"
            for key, name in (
                ("cash", "cash"),
                ("stock", "stock"),
                ("options", "options"),
                ("interest_accruals", "interest accruals"),
            )
            if row and row.get(key) is not None
        )
        if before and before["provenance"] == "statement" and Decimal(str(before["nav"])) == total:
            self.status = f"Statement NAV for {day} matches the recorded close ({total:,.2f})"
        else:
            service.ledger.import_close(
                day,
                total,
                f"{SOURCE} {payload['account']} NAV for {day}"
                + (f" ({parts})" if parts else "")
                + f"; fetched {now.isoformat()}",
                m.account,
            )
            service.force_optimize = True
            service.pending_target = None
            self.status = f"Statement NAV for {day} imported: {total:,.2f}"
            if before and before["provenance"] == "statement":
                service.alert(
                    "warning",
                    f"IBKR statement NAV for {day} ({total:,.2f}) differs from the entered close "
                    f"({Decimal(str(before['nav'])):,.2f}); the statement is now recorded. Check the entry.",
                    key=("flex_differs", str(day)),
                )
        provisional = Decimal(str(before["nav"])) if before and before["provenance"] == "api" else None
        service.alert(
            "info",
            f"IBKR statement for {day} imported automatically: NAV {total:,.2f}"
            + (f" ({parts})" if parts else "")
            + (
                f"; the app's provisional close was {provisional:,.2f} ({total - provisional:+,.2f})"
                if provisional
                else ""
            ),
            key=("flex_imported", str(day)),
        )
        self.last = {"date": str(day), "total": str(total), "at": now.isoformat()}
        return payload

    def api_accrued(self, day):
        """IBKR's AccruedCash as the app last captured it for `day` (close_marks payloads), or None."""
        rows = [
            r
            for r in self.service.store.records("close_marks", self.manifest.account, 40)
            if r.get("date") == str(day) and r.get("accrued_cash") not in (None, "")
        ]
        if not rows:
            return None
        return Decimal(str(max(rows, key=lambda r: r.get("at") or "")["accrued_cash"]))

    def state(self):
        rows = self.service.store.records("flex_statement", self.manifest.account, 10)
        latest = max(rows, key=lambda r: r.get("date", "")) if rows else None
        return {
            "configured": self.configured(),
            "status": self.status,
            "latest": {k: latest.get(k) for k in ("date", "total", "fetched_at")} if latest else None,
            "next_try": min((a + d for a, d in self.attempted.values())).isoformat()
            if self.attempted
            else None,
        }
