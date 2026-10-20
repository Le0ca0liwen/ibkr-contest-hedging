"""Exchange-close NAV capture, independent of quotes and trading mode."""

import asyncio
from datetime import timedelta
from decimal import Decimal

from .models import digest


class ClosingRecorder:
    def __init__(self, manifest, ledger, broker, clock, alert, demo=False):
        self.manifest, self.ledger, self.broker = manifest, ledger, broker
        self.clock, self.alert, self.demo = clock, alert, demo
        self.retry_at = None
        self.status = "Waiting for scheduled market close"
        self.last = None
        self.missed = set()
        self.loaded = False

    def state(self):
        now = self.clock()
        closes = self.ledger.closes()
        future = [
            self.manifest.session_close(day)
            for day in (self.manifest.day_zero, *self.manifest.scoring_sessions)
            if day not in closes and self.manifest.session_close(day) >= now
        ]
        return {
            "enabled": self.manifest.closing_capture.enabled,
            "status": self.status,
            "next_close": min(future).isoformat() if future else None,
            "last": self.last,
            "window_seconds": self.manifest.closing_capture.window_seconds,
        }

    async def tick(self):
        policy = self.manifest.closing_capture
        if not policy.enabled:
            self.status = "Automatic closing capture disabled"
            return
        # The service constructs components before initializing database tables.
        if not self.loaded:
            rows = self.ledger.store.records("closing_capture", self.manifest.account, 100)
            self.missed = {
                row["date"]
                for row in rows
                if row.get("contest") == self.manifest.contest_id and row.get("status") == "MISSED"
            }
            self.last = next(
                (
                    row
                    for row in rows
                    if row.get("contest") == self.manifest.contest_id and row.get("status") == "RECORDED"
                ),
                None,
            )
            self.loaded = True
        now = self.clock()
        closes = self.ledger.closes()
        for day in (self.manifest.day_zero, *self.manifest.scoring_sessions):
            close = self.manifest.session_close(day)
            if day in closes or now < close:
                continue
            deadline = close + timedelta(seconds=policy.window_seconds)
            if now > deadline:
                if str(day) not in self.missed:
                    self.ledger.store.record(
                        "closing_capture",
                        self.manifest.account,
                        {
                            "contest": self.manifest.contest_id,
                            "date": str(day),
                            "status": "MISSED",
                            "scheduled_close": close.isoformat(),
                            "reason": "Capture window missed; historical statement import required",
                        },
                    )
                    self.missed.add(str(day))
                    self.alert("warning", f"Closing NAV capture missed for {day}; import the IBKR statement")
                self.status = f"Closing capture missed for {day}; statement required"
                continue
            if self.retry_at is not None and now < self.retry_at:
                return
            self.retry_at = now + timedelta(seconds=policy.retry_seconds)
            self.status = f"Requesting closing NAV for {day}"
            try:
                observation = await self.broker.closing_nav_observation()
                value = Decimal(str(observation["nav"]))
                at = observation["observed_at"]
                requested = observation["requested_at"]
                expected_source = "synthetic" if self.demo else "ibkr"
                if (
                    observation["account"] != self.manifest.account
                    or observation["source"] != expected_source
                    or not value.is_finite()
                    or requested.tzinfo is None
                    or at.tzinfo is None
                    or not close <= requested <= at <= deadline
                    or at > self.clock() + timedelta(seconds=1)
                ):
                    raise ValueError("Invalid account, source, value or closing-observation timestamp")
                # Report imports can arrive while the broker request is pending.
                # No await separates this recheck and the synchronous ledger write.
                if day in self.ledger.closes():
                    self.status = f"Closing NAV for {day} already recorded; existing value retained"
                    return
                source = (
                    "SYNTHETIC simulator closing observation"
                    if self.demo
                    else "IBKR API closing observation (NetLiquidation; statement reconciliation pending)"
                ) + f"; scheduled {close.isoformat()}; observed {at.isoformat()}"
                self.ledger.import_close(day, value, source, self.manifest.account)
                self.last = {
                    "date": str(day),
                    "nav": str(value),
                    "observed_at": at.isoformat(),
                    "scheduled_close": close.isoformat(),
                    "source": source,
                }
                self.ledger.store.record(
                    "closing_capture",
                    self.manifest.account,
                    {**self.last, "contest": self.manifest.contest_id, "status": "RECORDED"},
                )
                self.status = f"Closing NAV recorded for {day}"
            except (ConnectionError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                self.status = f"Closing NAV capture waiting for retry: {exc}"
                self.alert("warning", self.status)
            return

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = f"Closing recorder unavailable: {type(exc).__name__}"
                try:
                    self.alert("error", self.status)
                except Exception:
                    pass
            await asyncio.sleep(1)


class CloseMarkRecorder:
    """Durable per-session close marks for score attribution (quotes, Greeks, IBKR marks, risk).

    During the final two minutes the latest capture is held in memory; the last one taken
    before the bell is written once the session closes, plus one post-close capture 30-300 s
    after the bell. Record ids are deterministic, so restarts never duplicate a session.
    """

    def __init__(self, manifest, store, clock, collect, alert):
        self.manifest, self.store, self.clock = manifest, store, clock
        self.collect, self.alert = collect, alert
        self.pending = None
        self.written = set()
        self.status = "Waiting for the final two minutes of a contest session"

    def _write(self, day, phase, payload):
        # Deterministic id: a restart inside the window cannot duplicate or overwrite a capture.
        identifier = digest((self.manifest.contest_id, str(day), phase))[:32]
        self.store.record("close_marks", self.manifest.account, payload, identifier)
        self.written.add((day, phase))
        self.status = f"Close marks recorded for {day} ({phase})"

    def tick(self):
        now = self.clock()
        day = self.manifest.active_session(now)
        if day is None:
            return
        seconds = (self.manifest.session_close(day) - now).total_seconds()
        if 0 < seconds <= 120:
            # Keep the last good capture; an empty one (no snapshot/quotes yet) never replaces it.
            if (payload := self.collect(now, day, "pre_close")) is not None:
                self.pending = (day, payload)
        elif seconds <= 0:
            if self.pending and self.pending[0] == day:
                self._write(day, "pre_close", self.pending[1])
                self.pending = None
            if -300 <= seconds <= -30 and (day, "post_close") not in self.written:
                if (payload := self.collect(now, day, "post_close")) is not None:
                    self._write(day, "post_close", payload)

    async def run(self):
        while True:
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = f"Close mark recorder unavailable: {type(exc).__name__}"
                try:
                    self.alert("warning", self.status)
                except Exception:
                    pass
            await asyncio.sleep(1)
