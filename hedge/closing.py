"""Exchange-close NAV capture, independent of quotes and trading mode."""

import asyncio
from datetime import timedelta
from decimal import Decimal

from .config import EXTENDED_HOURS, LATE_OPTION_CLOSE, LATE_OPTION_SYMBOLS, NY
from .execution_quality import timestamp
from .models import digest

# Source marker of the post-close capture, which stays an API row ("IBKR API ..."/"SYNTHETIC simulator ...").
POST_CLOSE = "post-close observation"
POST_CLOSE_WINDOW = timedelta(minutes=5)
# A hung broker request must not stall the recorder past its window.
OBSERVE_TIMEOUT_SECONDS = 60  # the app's request (15 s), then a side connection (10 s + 15 s)
# Contest: the instructor scores the IBKR Activity Statement NAV (Oct 23), which books the post-market
# session's trades (until 20:00 ET) but marks positions at the closing prices. Once that session has
# ended the recorder records the predicted statement NAV (hedge/statement.py) as the day's
# provisional close, superseding the 16:00 and 16:20 readings; the imported statement supersedes it.
END_OF_DAY = "end-of-day observation"
END_OF_DAY_DELAY = timedelta(minutes=5)
# Nothing trades after 20:00, so NAV stays put (Oct 22: the 20:05 reading equalled 21:29's): retry
# for two hours, and accept IBKR's pushed NetLiquidation when the summary request keeps failing.
END_OF_DAY_WINDOW = timedelta(hours=2)
PUSHED_AFTER = timedelta(minutes=2)  # a pushed NetLiquidation counts from 20:02 ET
# While the predicted statement NAV is still waiting (a restart before the first account snapshot,
# share closes still being fetched), IBKR's after-hours NetLiquidation is recorded only in the
# window's last half hour.
END_OF_DAY_FALLBACK = timedelta(minutes=30)


def post_close_window(manifest, day):
    """When post-close marks are taken: with the post-close NAV observation (close + late capture,
    for POST_CLOSE_WINDOW), so marks and the official NAV share SPY options' 16:15 close; 30-300 s
    after the bell when late capture is off (the bell NAV is then the official close)."""
    close = manifest.session_close(day)
    late = manifest.closing_capture.late_capture_minutes
    if late:
        start = close + timedelta(minutes=late)
        return start, start + POST_CLOSE_WINDOW
    return close + timedelta(seconds=30), close + timedelta(seconds=300)


class ClosingRecorder:
    """Records API NetLiquidation at the bell, then once more after the options close.

    An imported IBKR statement always outranks these API rows in the ledger and is never
    overwritten. Outside the contest the post-close observation follows a recorded bell capture
    only, so a missed bell requires the statement. In the contest (API close is official) a missed
    bell is retried once in the post-close window, checked against the last pre-close snapshot NAV.
    """

    def __init__(self, manifest, ledger, broker, clock, alert, demo=False, statement=None):
        self.manifest, self.ledger, self.broker = manifest, ledger, broker
        self.statement = statement  # now -> the service's predicted statement NAV (or None)
        self.clock, self.alert, self.demo = clock, alert, demo
        self.retry_at = None
        self.status = "Waiting for scheduled market close"
        self.last = None
        self.missed = set()
        self.post_close_done = set()  # diverged or skipped post-close observations (bell value kept)
        self.eod_missed = set()  # days whose end-of-day window passed without a reading
        self.loaded = False
        # The statement NAV is official (instructor, Oct 23); API readings are provisional.
        self.api_official = False
        # Contest: a provisional close after the post-market session, when its trades have booked.
        self.end_of_day = manifest.contest_rules == "spcx_rklb_spy"

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
            "late_capture_minutes": self.manifest.closing_capture.late_capture_minutes,
            "end_of_day": self.end_of_day_window(min(pending))[0].isoformat()
            if self.end_of_day and (pending := self._pending_end_of_day(now))
            else None,
        }

    def end_of_day_window(self, day):
        """Contest only: when the provisional end-of-day close (the predicted statement NAV) is
        recorded, from 5 minutes after the post-market session ends (20:05 ET), retried for two
        hours. None outside the contest."""
        if not self.end_of_day:
            return None
        start = self.manifest.session_close(day) + EXTENDED_HOURS + END_OF_DAY_DELAY
        return start, start + END_OF_DAY_WINDOW

    def _pending_end_of_day(self, now):
        revisions = self.ledger.revisions()
        return [
            day
            for day in (self.manifest.day_zero, *self.manifest.scoring_sessions)
            if self.end_of_day_window(day)[1] >= now and not self._end_of_day_settled(day, revisions)
        ]

    @staticmethod
    def _end_of_day_settled(day, revisions):
        """Its end-of-day reading is recorded, or the operator entered the day's close by hand."""
        return any(
            r["day"] == day and (END_OF_DAY in r["source"] or r["provenance"] == "statement")
            for r in revisions
        )

    def _post_close_settled(self, day, revisions):
        """A statement exists for the day, or its post-close observation is already recorded."""
        rows = [r for r in revisions if r["day"] == day]
        return not rows or any(r["provenance"] == "statement" or POST_CLOSE in r["source"] for r in rows)

    def _pre_close_nav(self, close):
        """NAV of the latest stored broker snapshot taken at or before the close, if any."""
        for row in self.ledger.store.records("snapshot", self.manifest.account, 300):
            try:
                if timestamp(row["at"]) <= close:
                    return Decimal(str(row["nav"]))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
        return None

    def _pushed_nav(self, close):
        """IBKR's latest pushed NetLiquidation (account-update feed) if it arrived after the
        post-market close, else None. The end of day's fallback when the summary request fails."""
        try:
            snapshot = self.broker.snapshot()
        except Exception:
            return None
        value = Decimal(str(snapshot.nav))
        if (
            snapshot.account != self.manifest.account
            or snapshot.account_at.tzinfo is None
            or snapshot.account_at < close + EXTENDED_HOURS + PUSHED_AFTER
            or snapshot.account_at > self.clock() + timedelta(seconds=1)
            or not value.is_finite()
            or value <= 0
        ):
            return None
        return value, snapshot.account_at

    async def _observe(self, start, deadline):
        observation = await asyncio.wait_for(self.broker.closing_nav_observation(), OBSERVE_TIMEOUT_SECONDS)
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
            or not start <= requested <= at <= deadline
            or at > self.clock() + timedelta(seconds=1)
        ):
            raise ValueError("Invalid account, source, value or closing-observation timestamp")
        return value, at

    async def tick(self):
        policy = self.manifest.closing_capture
        if not policy.enabled:
            self.status = "Automatic closing capture disabled"
            return
        # The service constructs components before initializing database tables.
        if not self.loaded:
            rows = self.ledger.store.records("closing_capture", self.manifest.account, 100)
            missed = [
                row
                for row in rows
                if row.get("contest") == self.manifest.contest_id and row.get("status") == "MISSED"
            ]
            self.missed = {row["date"] for row in missed if row.get("phase") != "end_of_day"}
            self.eod_missed = {row["date"] for row in missed if row.get("phase") == "end_of_day"}
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
        revisions = self.ledger.revisions()
        closes = self.ledger.close_records(revisions)
        for day in (self.manifest.day_zero, *self.manifest.scoring_sessions):
            close = self.manifest.session_close(day)
            if now < close:
                continue
            eod = self.end_of_day_window(day)
            eod_due = eod is not None and now >= eod[0] and not self._end_of_day_settled(day, revisions)
            if eod_due and now > eod[1]:
                if str(day) not in self.eod_missed:
                    self.eod_missed.add(str(day))
                    self.ledger.store.record(
                        "closing_capture",
                        self.manifest.account,
                        {
                            "contest": self.manifest.contest_id,
                            "date": str(day),
                            "status": "MISSED",
                            "phase": "end_of_day",
                            "scheduled_close": eod[0].isoformat(),
                            "reason": "End-of-day window missed",
                        },
                    )
                    self.alert(
                        "warning",
                        f"End-of-day NAV capture missed for {day}; enter the IBKR statement NAV for {day} "
                        "in Settings & alerts > Import an official closing NAV when it is published",
                    )
                self.status = f"End-of-day NAV capture missed for {day}; enter the end-of-day NAV manually"
                continue
            if eod_due:
                phase, start, deadline = "end_of_day", *eod
            elif day in closes:
                start = close + timedelta(minutes=policy.late_capture_minutes)
                deadline = start + POST_CLOSE_WINDOW
                settled = day in self.post_close_done or self._post_close_settled(day, revisions)
                if policy.late_capture_minutes and now > deadline and not settled:
                    self.post_close_done.add(day)
                    self.status = f"Post-close observation skipped for {day}; bell capture retained"
                if not policy.late_capture_minutes or not start <= now <= deadline or settled:
                    continue
                phase = "post_close"
            else:
                phase = "close"
                start, deadline = close, close + timedelta(seconds=policy.window_seconds)
                late = close + timedelta(minutes=policy.late_capture_minutes)
                retry_late = (
                    self.api_official
                    and policy.late_capture_minutes
                    and now <= late + POST_CLOSE_WINDOW
                    and str(day) not in self.missed
                )
                if now > deadline and retry_late:
                    # Contest: the post-close API reading is official too, so a missed bell gets
                    # one more chance after the options close.
                    if now < late:
                        self.status = (
                            f"Closing NAV capture missed the bell for {day}; retrying after the "
                            f"options close ({late.astimezone(NY):%H:%M} ET)"
                        )
                        continue
                    phase, start, deadline = "post_close", late, late + POST_CLOSE_WINDOW
                elif now > deadline:
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
                        self.alert(
                            "warning",
                            f"16:00 closing NAV capture missed for {day}; the predicted statement NAV is "
                            "still recorded after the 20:00 ET post-market close, and the IBKR statement "
                            "decides"
                            if self.end_of_day
                            else f"Closing NAV capture missed for {day}; import the IBKR statement",
                        )
                    self.status = f"Closing capture missed for {day}; " + (
                        "end-of-day estimate pending, statement decides"
                        if self.end_of_day
                        else "statement required"
                    )
                    continue
            fallback = phase == "post_close" and day not in closes
            if self.retry_at is not None and now < self.retry_at:
                return
            self.retry_at = now + timedelta(seconds=policy.retry_seconds)
            label = {"post_close": "Post-close NAV", "end_of_day": "End-of-day NAV"}.get(phase, "Closing NAV")
            self.status = f"{label} requested for {day}"
            try:
                pushed = predicted = False
                estimate = None
                if phase == "end_of_day" and self.statement:
                    try:
                        estimate = self.statement(now)
                    except Exception as exc:  # never blocks the NetLiquidation fallback below
                        estimate = {"status": "waiting", "reason": f"{type(exc).__name__}: {exc}"}
                if estimate and estimate.get("status") == "ok":
                    # The statement's own figure: cash after the post-market session, positions at the
                    # closing marks. IBKR's after-hours NetLiquidation is not what is scored.
                    value, at, predicted = Decimal(str(round(estimate["value"], 2))), now, True
                elif estimate and now < deadline - END_OF_DAY_FALLBACK:
                    self.status = (
                        f"End-of-day NAV for {day} waits for the predicted statement NAV: "
                        f"{estimate.get('reason') or estimate.get('status')}"
                    )
                    return
                else:
                    try:
                        value, at = await self._observe(start, deadline)
                    except (ConnectionError, TimeoutError, ValueError, KeyError, TypeError):
                        # End of day only: the pushed NetLiquidation is the same figure (nothing trades
                        # after 20:00), so a failing summary request cannot cost the day.
                        reading = self._pushed_nav(close) if phase == "end_of_day" else None
                        if reading is None:
                            raise
                        (value, at), pushed = reading, True
                # Report imports can arrive while the broker request is pending.
                # No await separates this recheck and the synchronous ledger write.
                if (phase == "close" or fallback) and day in self.ledger.closes():
                    self.status = f"Closing NAV for {day} already recorded; existing value retained"
                    return
                if phase == "end_of_day" and self._end_of_day_settled(day, self.ledger.revisions()):
                    self.status = f"Close for {day} already recorded; end-of-day observation discarded"
                    return
                if (
                    phase == "post_close"
                    and not fallback
                    and self._post_close_settled(day, self.ledger.revisions())
                ):
                    self.status = (
                        f"Statement NAV for {day} already recorded; post-close observation discarded"
                    )
                    return
                bell = closes[day]["nav"] if phase == "post_close" and not fallback else None
                limit = Decimal(str(policy.late_capture_max_delta)) * self.manifest.initial_nav
                reference = self._pre_close_nav(close) if fallback else None
                if reference is not None and abs(value - reference) > limit:
                    # No bell to compare with: a reading far from the last pre-close NAV is not used.
                    self.missed.add(str(day))
                    self.ledger.store.record(
                        "closing_capture",
                        self.manifest.account,
                        {
                            "contest": self.manifest.contest_id,
                            "date": str(day),
                            "status": "MISSED",
                            "phase": "post_close",
                            "nav": str(value),
                            "pre_close_nav": str(reference),
                            "scheduled_close": close.isoformat(),
                            "reason": "Bell missed; post-close reading diverged from the pre-close NAV",
                        },
                    )
                    self.status = (
                        f"Closing capture missed for {day}; post-close NAV differed from the last "
                        f"pre-close NAV by {value - reference:+.2f}"
                    )
                    self.alert(
                        "warning",
                        f"{self.status}. Enter IBKR's closing NAV in Settings & alerts > Import an "
                        "official closing NAV",
                    )
                    return
                if bell is not None and abs(value - bell) > limit:
                    # Likely extended-hours stock marks: keep the bell value, ask for the statement.
                    self.post_close_done.add(day)
                    self.ledger.store.record(
                        "closing_capture",
                        self.manifest.account,
                        {
                            "contest": self.manifest.contest_id,
                            "date": str(day),
                            "status": "DIVERGED",
                            "phase": "post_close",
                            "nav": str(value),
                            "bell_nav": str(bell),
                        },
                    )
                    self.status = f"Post-close NAV for {day} diverged from the bell by {value - bell:+.2f}"
                    self.alert(
                        "warning",
                        f"{self.status}; bell value kept. "
                        + (
                            f"Check IBKR's closing NAV for {day} and correct it in Settings & alerts if it "
                            "differs"
                            if self.api_official
                            else f"Import the IBKR statement NAV for {day}"
                        ),
                    )
                    return
                if phase == "end_of_day":
                    extended = (close + EXTENDED_HOURS).astimezone(NY)
                    source = (
                        f"SYNTHETIC simulator {END_OF_DAY}"
                        + (" (predicted statement NAV)" if predicted else "")
                        if self.demo
                        else f"IBKR API {END_OF_DAY} (predicted statement NAV after the {extended:%H:%M} ET "
                        "post-market close: cash and accrued interest plus positions at the statement's "
                        "closing marks (shares 16:00 close, options closing midpoint); provisional until "
                        "the IBKR statement is imported)"
                        if predicted
                        else f"IBKR API {END_OF_DAY} ("
                        + ("account update " if pushed else "")
                        + f"after the {extended:%H:%M} ET post-market close; NetLiquidation, closing marks "
                        "unavailable; provisional until the IBKR statement is imported)"
                    )
                elif phase == "post_close":
                    options_close = (close + timedelta(minutes=15)).astimezone(NY)
                    source = (
                        f"SYNTHETIC simulator {POST_CLOSE} (after {options_close:%H:%M} options close)"
                        if self.demo
                        else f"IBKR API {POST_CLOSE} (after {options_close:%H:%M} options close; "
                        + (
                            "NetLiquidation; official close)"
                            if self.api_official
                            else "NetLiquidation; statement reconciliation pending)"
                        )
                    )
                    if fallback:
                        source += "; bell capture missed"
                else:
                    source = (
                        "SYNTHETIC simulator closing observation"
                        if self.demo
                        else "IBKR API closing observation (NetLiquidation; "
                        + ("official close)" if self.api_official else "statement reconciliation pending)")
                    )
                source += f"; scheduled {start.isoformat()}; observed {at.isoformat()}"
                self.ledger.import_close(day, value, source, self.manifest.account)
                self.last = {
                    "date": str(day),
                    "nav": str(value),
                    "observed_at": at.isoformat(),
                    "scheduled_close": close.isoformat(),
                    "phase": phase,
                    "source": source,
                }
                # A predicted statement NAV keeps its parts (cash, accrued, shares, options, each
                # leg's mark and estimates, interest), for comparison with the imported statement.
                prediction = (
                    {
                        k: estimate[k]
                        for k in ("value", "parts", "interest", "low", "high", "base_at", "check")
                        if k in estimate
                    }
                    if predicted
                    else None
                )
                self.ledger.store.record(
                    "closing_capture",
                    self.manifest.account,
                    {
                        **self.last,
                        "contest": self.manifest.contest_id,
                        "status": "RECORDED",
                        **({"prediction": prediction} if prediction else {}),
                    },
                )
                self.status = f"{label} recorded for {day}"
            except (ConnectionError, TimeoutError, ValueError, KeyError, TypeError) as exc:
                self.status = f"{label} capture waiting for retry: {exc or type(exc).__name__}"
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
    before the bell is written once the session closes, plus one post-close capture in the
    post-close NAV observation's window (post_close_window): IBKR's marks then include SPY
    options' 16:15 close, as the official post-close NAV does. Record ids are deterministic, so
    restarts never duplicate a session.
    """

    def __init__(self, manifest, store, clock, collect, alert):
        self.manifest, self.store, self.clock = manifest, store, clock
        self.collect, self.alert = collect, alert
        self.pending = None
        self.pending_late = None  # the latest capture before SPY options' 16:15 close
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
        # SPY-style options close (and are marked on the statement) at 16:15: the last capture
        # before then, when the contract holds such options.
        late = self.manifest.session_close(day) + LATE_OPTION_CLOSE
        late_seconds = (late - now).total_seconds()
        if any(c.kind == "OPT" and c.symbol in LATE_OPTION_SYMBOLS for c in self.manifest.contracts):
            if 0 < late_seconds <= 120:
                if (payload := self.collect(now, day, "options_close")) is not None:
                    self.pending_late = (day, payload)
            elif late_seconds <= 0 and self.pending_late and self.pending_late[0] == day:
                self._write(day, "options_close", self.pending_late[1])
                self.pending_late = None
        if 0 < seconds <= 120:
            # Keep the last good capture; an empty one (no snapshot/quotes yet) never replaces it.
            if (payload := self.collect(now, day, "pre_close")) is not None:
                self.pending = (day, payload)
        elif seconds <= 0:
            if self.pending and self.pending[0] == day:
                self._write(day, "pre_close", self.pending[1])
                self.pending = None
            start, end = post_close_window(self.manifest, day)
            if start <= now <= end and (day, "post_close") not in self.written:
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
