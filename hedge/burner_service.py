"""Integrated closing surplus burner: strictly sequential IOC round trips inside TradingService.

One cycle at a time, durable at every step:

    IDLE -> ENTRY (IOC buy q at the ask) -> settle (entry terminal, fills recorded, positions
    agree) -> EXIT (IOC sell the CONFIRMED fill at the bid less a widening collar, retried)
    -> CLOSED, or RESIDUAL (alerted once; the inventory becomes an ordinary holding).

In the contest the window ends at the scored 20:00 ET post-market close (shares only). IBKR does
not accept IOC outside regular hours, so there each leg is a DAY limit that the burner cancels
DAY_LEG_SECONDS after sending: the same fill-what-is-there-now leg.

Records: 'burn_cycle' (insert-once plan, id=cycle_id), append-only 'burn_cycle_event',
'burn_cycle_result' (final; releases the hedge block) and throttled 'burn_decision'.
Fill quantities always come from Store.effective_fills by order reference, never from intent
state (a partially filled IOC ends CANCELLED). See docs/CLOSING_BURNER.md.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from .burner import (
    candidates,
    capacity_per_minute,
    closing_gaps,
    cycle_seconds,
    fill_efficiency,
    gap_stats,
    hysteresis,
    jit_waiting,
    option_commission,
    plan_cap,
    select,
    stock_commission,
    weighted_quantile,
)
from .config import NY
from .continuity import DataUnavailable
from .execution_quality import timestamp
from .storage import TERMINAL, burn_result_id

EXIT_MARGIN_SECONDS = 15  # stop sending exits this long before the ordinary order cutoff
LEG_TIMEOUT_SECONDS = 10  # an IOC still non-terminal after this triggers one reconciliation
SETTLE_SECONDS = 5  # bounded wait for positions to match confirmed fills before reconciling
# An IOC that ended short of its quantity may have been reported Cancelled (ib_async does this
# on error 202 with the last known filled, often 0) before its execution reports and position
# update arrived. Wait this long, then download executions, completed orders and positions once
# before trusting "unfilled" (a CLOSED result or another exit attempt).
QUIET_SECONDS = 1.0
# An exit that ended short is confirmed by this many downloads before another exit is sized from
# the recorded fills: selling a late-reported fill again would leave the account short.
EXIT_CONFIRMATIONS = 2
# An exit waits for an ordinary spread (the entry's execution spread limits) until this long
# before its deadline, then sells at the bid regardless rather than carry the inventory.
EXIT_SPREAD_GRACE_SECONDS = 30
EXIT_SPREAD_WAIT_SECONDS = 20  # a wide book that lasts longer is the market, not a glitch
SCENARIO_AGE_SECONDS = 20
DAY_LEG_SECONDS = 3  # an extended-hours DAY leg's unfilled rest is cancelled this long after sending
NAV_STREAM = "IBKR P&L stream (1 s)"  # NAVTracker.source when rolled from a settled push
DECISION_RECORD_SECONDS = 15
MODES = ("off", "shadow", "paper")


def _iso(value):
    return value.isoformat() if value is not None else None


def _et(value):
    return value.astimezone(NY).strftime("%H:%M:%S ET")


class ClosingBurner:
    def __init__(self, service):
        self.service = service
        self.wakeup = asyncio.Event()
        self.override = None  # operator downgrade for NEW entries ("off"/"shadow"); never upgrades
        self.active = False
        self.active_day = None
        self.state_name = "OFF"
        self.reason = "Burner disabled in configuration"
        self.last = {}
        self.cycle = None
        self.leg_ids = set()
        self.reported = {}
        self.mismatch = None
        self.reconcile_requested = set()
        self.verified = set()  # short-filled legs whose fills a later reconcile confirmed
        self.confirmations = {}  # downloads made for a short-filled leg
        self.finished_legs = {}  # leg id -> label of a final cycle (late fills are alerted)
        # Fills a final cycle already counted. Every reconcile replays the day's executions, so only
        # an execution not seen before is a late fill.
        self.known_exec_ids = set()
        self.exit_wait_since = None  # first wide-book refusal of the current exit
        self.cancel_requested = {}  # extended-hours DAY leg id -> when its cancel was last sent
        self.cancel_failed = set()  # legs whose failed cancel was already recorded
        # Session -> spend budget (burned so far + what is still needed), lowered at every planned
        # cycle and never raised: a forecast that drifts up on noise cannot ratchet spending up.
        self.budgets = {}
        self.quiet_seconds = QUIET_SECONDS
        self.loaded = False
        self.burn_epoch = 0
        self._decision_key = None
        self._decision_at = 0.0
        self._monitor_wake_at = 0.0
        self._shadow_planned_at = 0.0
        self._stats = None
        self._exit_refusal = None
        # Session date -> True when an approved event caution turned the burner off for that session
        # (service wires EventRisk.burner_off). Downgrade-only; exits_allowed() ignores it.
        self.event_off = lambda day: False

    # ------------------------------------------------------------------ plumbing
    @property
    def manifest(self):
        return self.service.manifest

    @property
    def policy(self):
        return self.service.manifest.burner

    @property
    def store(self):
        return self.service.store

    @property
    def account(self):
        return self.service.manifest.account

    def wake(self):
        self.wakeup.set()

    async def wait(self, timeout=1.0):
        try:
            await asyncio.wait_for(self.wakeup.wait(), timeout)
        except TimeoutError:
            pass
        self.wakeup.clear()

    def on_broker_event(self, event, value):
        """Called by the service event loop; never raises."""
        try:
            if event == "order":
                reference = value.get("order_ref") or ""
                if reference.startswith("hedge:"):
                    identifier = reference.removeprefix("hedge:")
                    filled = int(float(value.get("filled") or 0))
                    self.reported[identifier] = max(self.reported.get(identifier, 0), filled)
                    if self.cycle is not None:
                        self.wake()
            elif (
                event == "execution"
                and value.order_ref.removeprefix("hedge:") in self.finished_legs
                and value.exec_id not in self.known_exec_ids
            ):
                self.known_exec_ids.add(value.exec_id)
                label = self.finished_legs[value.order_ref.removeprefix("hedge:")]
                self.service.alert(
                    "error",
                    f"Late fill on a closing burner leg after its cycle ended: {value.signed_quantity:+d} "
                    f"{label}. It is ordinary inventory now; check the position on Positions & risk",
                )
            if event in ("execution", "commission", "position") and self.cycle is not None:
                self.wake()
        except Exception:
            pass

    def restore_setting(self):
        """Re-apply the operator's last downgrade (restart-safe, across configuration edits).

        configured_mode() clamps it to the configured mode, so it can only ever downgrade.
        """
        for record in self.store.records("burner_setting", self.account, 1):
            self.override = record.get("override")

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError("Burner mode must be off, shadow or paper")
        if MODES.index(mode) > MODES.index(self.policy.mode):
            raise ValueError(f"Configuration allows at most burner mode '{self.policy.mode}'")
        self.override = None if mode == self.policy.mode else mode
        self.store.record(
            "burner_setting",
            self.account,
            {"override": self.override, "mode": mode, "config_hash": self.manifest.config_hash},
        )
        self.wake()
        return self.state()

    def event_off_now(self):
        """Approved event caution: burner off for today's session only (never raises)."""
        try:
            day = self.manifest.active_session(self.service.now())
            return day is not None and bool(self.event_off(day))
        except Exception:
            return False

    def configured_mode(self):
        if self.event_off_now():
            return "off"  # can only lower the mode; the persistent override is untouched
        if self.override is None:
            return self.policy.mode
        return MODES[min(MODES.index(self.override), MODES.index(self.policy.mode))]

    def effective_mode(self):
        """'paper' only when both the burner and the app are in paper; else shadow or off."""
        mode = self.configured_mode()
        if mode == "off":
            return "off"
        return "paper" if mode == "paper" and self.service.mode == "PAPER" else "shadow"

    def exits_allowed(self):
        """Restoring an open cycle ignores the entry override but never the app's paper state."""
        s, ex = self.service, self.service.execution
        return (
            self.policy.mode == "paper"
            and s.mode == "PAPER"
            and not ex.paused
            and not ex.pause_requested
            and not s.broker.readonly
            and s.data_wait is None
        )

    def window(self, day):
        """Times around the scored close: the regular close, or 20:00 ET in the contest, where the
        window falls in the post-market session and options (regular hours only) never open."""
        m, p = self.manifest, self.policy
        close, regular = m.scoring_close(day), m.session_close(day)
        start = close - timedelta(minutes=p.window_minutes)
        return {
            "close": close,
            "start": start,
            "stock_entry_cutoff": close - timedelta(minutes=p.stock_entry_cutoff_minutes),
            "option_entry_cutoff": (
                close - timedelta(minutes=p.option_entry_cutoff_minutes) if close == regular else start
            ),
            "stock_exit_deadline": close
            - timedelta(minutes=m.policy.stock_cutoff_minutes, seconds=EXIT_MARGIN_SECONDS),
            "option_exit_deadline": regular
            - timedelta(minutes=m.policy.option_cutoff_minutes, seconds=EXIT_MARGIN_SECONDS),
        }

    def _set(self, state, reason):
        self.state_name, self.reason = state, reason

    def _event(self, cycle, event, **values):
        self.store.record(
            "burn_cycle_event",
            self.account,
            {
                "cycle_id": cycle["cycle_id"],
                "session": cycle["session"],
                "event": event,
                "at": self.service.now().isoformat(),
                "seq": time.monotonic_ns(),  # orders events that share a timestamp
                **values,
            },
        )

    def _request_scenarios(self):
        if time.monotonic() - self._monitor_wake_at >= 1:
            self._monitor_wake_at = time.monotonic()
            wakeup = getattr(self.service, "monitor_wakeup", None)
            if wakeup is not None:
                wakeup.set()

    # ------------------------------------------------------------------ main step
    async def step(self):
        now = self.service.now()
        if not self.loaded:
            self._load_open_cycle(rehydrated=True)
            self.loaded = True  # only after a successful load; a failed read retries next step
        try:
            if self.cycle is not None:
                await self._advance(now)
                return
            await self._evaluate(now)
        except (DataUnavailable, ConnectionError, TimeoutError) as exc:
            # Missing broker/account data or a slow reconcile is a wait, never a burner fault.
            self._set("WAITING", f"Broker or data unavailable: {str(exc) or type(exc).__name__}")

    def _load_open_cycle(self, rehydrated=False):
        ids = self.store.open_burn_cycle_ids(self.account)
        cycles = [self.store.get_record(i, self.account, "burn_cycle") for i in ids]
        cycles = sorted((c for c in cycles if c), key=lambda c: c["planned_at"])
        if not cycles:
            return
        self.cycle = cycles[0]
        self.leg_ids = {leg["id"] for leg in self.store.burn_legs(self.account, self.cycle["cycle_id"])}
        if rehydrated:
            self._event(self.cycle, "REHYDRATED", legs=sorted(self.leg_ids))
            self._set("RECOVERING", "Open burn cycle restored from durable records")

    # ------------------------------------------------------------------ open cycle
    async def _advance(self, now):
        s, c, p = self.service, self.cycle, self.policy
        legs = self.store.burn_legs(self.account, c["cycle_id"])
        self.leg_ids |= {leg["id"] for leg in legs}
        fills = self.store.fills_for_refs(self.account, ["hedge:" + leg["id"] for leg in legs])
        net = sum(f["signed_quantity"] for f in fills)
        w = self.window(date.fromisoformat(c["session"]))
        deadline = w["option_exit_deadline"] if c["kind"] == "OPT" else w["stock_exit_deadline"]
        pending = [leg for leg in legs if leg["state"] not in TERMINAL]
        if pending:
            if any(leg["state"] == "UNKNOWN" for leg in pending):
                self._set("HALTED", "Uncertain burn leg; broker reconciliation required before any exit")
                if now >= w["close"]:
                    self._finalize("RESIDUAL", "Unresolved burn leg at the close", legs, fills, now)
                return
            leg = pending[-1]
            closed = now >= w["close"]
            if closed and leg["id"] in self.reconcile_requested:
                # Downloaded once and still not final: an IOC cannot fill after the close, so
                # release the hedger instead of blocking it into the next session.
                self._finalize("RESIDUAL", "Burn leg not final at the close", legs, fills, now)
                return
            self._set("ENTRY" if leg["leg"] == "entry" else "EXIT", f"Waiting for the {leg['leg']} IOC")
            age = (now - timestamp(leg["created_at"])).total_seconds()
            sent = s.execution.sent_at.get(leg["id"])
            resting = (now - sent if sent is not None else timedelta(seconds=age)).total_seconds()
            requested = self.cancel_requested.get(leg["id"])
            if (
                leg.get("time_in_force") == "DAY"
                and leg["broker_order_id"] is not None
                and resting >= DAY_LEG_SECONDS
                and (requested is None or (now - requested).total_seconds() >= LEG_TIMEOUT_SECONDS)
            ):
                # Extended hours: cancel what did not fill at once, exactly as an IOC would. A cancel
                # that failed is retried on the next step, one that went unanswered after the timeout.
                self.cancel_requested[leg["id"]] = now
                if leg["id"] not in self.cancel_failed:  # retries while disconnected stay quiet
                    self._event(c, "CANCEL", intent_id=leg["id"], reason="Extended-hours DAY leg rest")
                try:
                    await s.execution.cancel_order(leg["broker_order_id"])
                except Exception as exc:  # e.g. disconnected, or already final
                    self.cancel_requested.pop(leg["id"], None)
                    if leg["id"] not in self.cancel_failed:
                        self.cancel_failed.add(leg["id"])
                        self._event(c, "CANCEL_FAILED", intent_id=leg["id"], reason=str(exc))
            if (closed or age > LEG_TIMEOUT_SECONDS) and leg["id"] not in self.reconcile_requested:
                self.reconcile_requested.add(leg["id"])
                self._event(c, "RECONCILE", intent_id=leg["id"], reason="IOC leg not terminal")
                try:
                    await s.reconcile()
                except Exception:
                    if closed:
                        self._finalize("RESIDUAL", "Burn leg unverified at the close", legs, fills, now)
                        return
                    self.reconcile_requested.discard(leg["id"])  # retry on a later step
                    raise
            return
        # The most recently transmitted leg (IB order ids increase per client).
        transmitted = [leg for leg in legs if leg["broker_order_id"] is not None]
        last = max(transmitted, key=lambda leg: leg["broker_order_id"], default=None)
        if last is not None and last["id"] not in self.verified:
            done = abs(sum(f["signed_quantity"] for f in fills if f["order_ref"] == "hedge:" + last["id"]))
            if done < abs(last["signed_quantity"]):
                self._set("SETTLING", f"Confirming the {last['leg']} IOC's final fills")
                self._event(c, "RECONCILE", intent_id=last["id"], reason="IOC ended short of its quantity")
                await asyncio.sleep(self.quiet_seconds)
                try:
                    await s.reconcile()
                except Exception:
                    if now >= w["close"]:
                        self._finalize(
                            "RESIDUAL", "Final IOC fills unverified at the close", legs, fills, now
                        )
                        return
                    raise
                self.confirmations[last["id"]] = self.confirmations.get(last["id"], 0) + 1
                if last["leg"] == "entry" or self.confirmations[last["id"]] >= EXIT_CONFIRMATIONS:
                    self.verified.add(last["id"])
                # Recompute from the downloaded executions and positions.
                return await self._advance(s.now())
        # Settled only when every broker-confirmed fill is recorded AND positions agree with them.
        unreported = any(
            abs(sum(f["signed_quantity"] for f in fills if f["order_ref"] == "hedge:" + leg["id"]))
            < self.reported.get(leg["id"], 0)
            for leg in legs
        )
        actual = s.broker.snapshot().quantities().get(c["con_id"], 0)
        if unreported or actual != c["original_quantity"] + net:
            problem = (
                "Waiting for execution reports the broker already confirmed"
                if unreported
                else "Waiting for broker positions to match confirmed fills"
            )
            if self.mismatch is None:
                self.mismatch = {"since": now, "reconciled": False}
            if (now - self.mismatch["since"]).total_seconds() < SETTLE_SECONDS:
                self._set("SETTLING", problem)
            elif not self.mismatch["reconciled"]:
                self._event(c, "RECONCILE", reason=problem)
                try:
                    await s.reconcile()  # a failed reconcile retries; it never counts as reconciled
                except Exception:
                    if now >= w["close"]:
                        self._finalize("RESIDUAL", "Fills unverified at the close", legs, fills, now)
                        return
                    raise
                self.mismatch = {"since": s.now(), "reconciled": True}
            else:
                self._finalize(
                    "RESIDUAL",
                    "Fills and broker position still disagree after reconciliation",
                    legs,
                    fills,
                    now,
                )
            return
        self.mismatch = None
        if net == 0:
            transmitted = any(leg["broker_order_id"] is not None for leg in legs)
            reason = (
                "Round trip complete"
                if fills
                else "Entry IOC unfilled"
                if transmitted
                else "Entry not transmitted"
            )
            self._finalize("CLOSED", reason, legs, fills, now)
            return
        if net < 0:
            self._finalize("RESIDUAL", "Exit sold more than the confirmed increment", legs, fills, now)
            return
        attempts = sum(1 for leg in legs if leg["leg"] == "exit" and leg["broker_order_id"] is not None)
        if attempts >= p.max_exit_attempts:
            self._finalize("RESIDUAL", f"Exit attempts exhausted ({attempts})", legs, fills, now)
            return
        if now >= deadline:
            self._finalize("RESIDUAL", "Order window closing before the exit completed", legs, fills, now)
            return
        if p.mode != "paper":
            # Restarted under a configuration that no longer permits burner trading.
            self._finalize(
                "RESIDUAL", "Burner is not configured for paper; exit not permitted", legs, fills, now
            )
            return
        if not self.exits_allowed():
            self._set("EXIT_BLOCKED", "Paper execution unavailable; exit waits")
            s.alert(
                "warning",
                f"Closing burner holds {net} temporary {c['label']} (cycle {c['cycle_id'][:8]}). It is "
                f"restored automatically if paper trading resumes before {_et(deadline)}; otherwise it "
                "becomes ordinary inventory. No liquidation is sent while paused.",
                key=("burner_exit_blocked", c["session"], c["cycle_id"]),
            )
            return
        scenarios = s.last_scenarios
        if scenarios is None or c["con_id"] not in scenarios.ids:
            self._set("EXIT", "Waiting for a scenario set covering the burn instrument")
            self._request_scenarios()
            return
        collar = p.exit_collar_ticks[min(attempts, len(p.exit_collar_ticks) - 1)]
        async with s.reconcile_lock:
            result = await s.execution.submit_burn_leg(
                cycle_id=c["cycle_id"],
                leg="exit",
                attempt=attempts,
                con_id=c["con_id"],
                signed=-net,
                original_quantity=c["original_quantity"],
                decision_id=c["cycle_id"],
                snapshot=s.broker.snapshot(),
                quotes=s.broker.quotes(),
                scenarios=scenarios,
                now=now,
                quote_age=p.quote_age_seconds,
                collar_ticks=collar,
                reserve=c["expected_loss"],
                deadline=deadline,
                # Wait out a momentarily wide book (at most EXIT_SPREAD_WAIT_SECONDS), and never
                # carry the inventory into the last seconds of the window.
                spread_guard=(deadline - now).total_seconds() > EXIT_SPREAD_GRACE_SECONDS
                and (now - self.exit_wait_since).total_seconds() < EXIT_SPREAD_WAIT_SECONDS
                if self.exit_wait_since is not None
                else (deadline - now).total_seconds() > EXIT_SPREAD_GRACE_SECONDS,
            )
        if result.get("intent_id"):
            self.leg_ids.add(result["intent_id"])
        if result["submitted"]:
            self._exit_refusal = self.exit_wait_since = None
            self._event(
                c,
                "EXIT_SUBMITTED",
                intent_id=result["intent_id"],
                attempt=attempts,
                collar_ticks=collar,
                quantity=-net,
                limit=result["limit"],
            )
            self._set("EXIT", f"Exit attempt {attempts + 1} sent ({collar} tick collar)")
        elif result.get("uncertain"):
            self._event(c, "EXIT_UNCERTAIN", intent_id=result["intent_id"], reason=result["reason"])
            self._set("HALTED", result["reason"])
        else:
            if "spread wider" in result["reason"] and self.exit_wait_since is None:
                self.exit_wait_since = now
            if self._exit_refusal != result["reason"]:
                self._exit_refusal = result["reason"]
                self._event(c, "EXIT_REFUSED", attempt=attempts, reason=result["reason"])
            self._set("EXIT", "Exit waiting: " + result["reason"])

    def _summarize(self, c, legs, fills, status, reason, now):
        multiplier = c.get("multiplier", 1)
        kinds = {leg["id"]: leg["leg"] for leg in legs}

        def side(name):
            rows = [f for f in fills if kinds.get(f["order_ref"].removeprefix("hedge:")) == name]
            units = sum(abs(f["signed_quantity"]) for f in rows)
            value = sum(Decimal(str(f["price"])) * abs(f["signed_quantity"]) for f in rows)
            return rows, units, (value / units if units else None)

        entry_rows, entry_units, entry_vwap = side("entry")
        exit_rows, exit_units, exit_vwap = side("exit")
        residual = sum(f["signed_quantity"] for f in fills)
        # Net cash paid for the cycle's fills: buys add, sells subtract (a round trip that buys at
        # the ask and sells at the bid pays the spread, a positive loss).
        net_paid = sum(Decimal(str(f["price"])) * f["signed_quantity"] * multiplier for f in fills)
        known = [f["commission"] for f in fills if f["commission"] is not None]
        fees = sum((Decimal(str(x)) for x in known), Decimal(0))
        # Loss on the round-tripped part; residual inventory is carried at its entry VWAP.
        carried = Decimal(residual) * entry_vwap * multiplier if residual and entry_vwap else Decimal(0)
        realized = net_paid - carried + fees
        planned = datetime.fromisoformat(c["planned_at"])
        first_entry = min((timestamp(f["at"]) for f in entry_rows), default=None)
        last_exit = max((timestamp(f["at"]) for f in exit_rows), default=None)
        entry_legs = [leg for leg in legs if leg["leg"] == "entry"]
        exit_legs = [leg for leg in legs if leg["leg"] == "exit"]
        return {
            "cycle_id": c["cycle_id"],
            "session": c["session"],
            "status": status,
            "reason": reason,
            "con_id": c["con_id"],
            "label": c["label"],
            "kind": c["kind"],
            "multiplier": multiplier,
            "original_quantity": c["original_quantity"],
            "planned_quantity": c["quantity"],
            "transmitted": any(leg["broker_order_id"] is not None for leg in entry_legs),
            "entry_filled": entry_units,
            "exit_filled": exit_units,
            "residual": residual,
            "entry_vwap": float(entry_vwap) if entry_vwap is not None else None,
            "exit_vwap": float(exit_vwap) if exit_vwap is not None else None,
            "realized_loss": float(realized),
            "fees": float(fees),
            "fees_pending": len(known) != len(fills),
            "expected_loss": c["expected_loss"],
            "turnover": float(sum(Decimal(str(f["price"])) * abs(f["signed_quantity"]) for f in fills))
            * multiplier,
            "exit_attempts": sum(1 for leg in exit_legs if leg["broker_order_id"] is not None),
            "intent_ids": [leg["id"] for leg in legs],
            "entry_latency_s": (
                (first_entry - timestamp(entry_legs[0]["created_at"])).total_seconds()
                if first_entry and entry_legs
                else None
            ),
            "exit_latency_s": (
                (last_exit - timestamp(exit_legs[0]["created_at"])).total_seconds()
                if last_exit and exit_legs
                else None
            ),
            "cycle_seconds": max(0.0, (now - planned).total_seconds()),
            "planned_at": c["planned_at"],
            "finished_at": now.isoformat(),
        }

    def _finalize(self, status, reason, legs, fills, now):
        s, c = self.service, self.cycle
        result = self._summarize(c, legs, fills, status, reason, now)
        self.store.record("burn_cycle_result", self.account, result, burn_result_id(c["cycle_id"]))
        self._event(c, status, reason=reason, residual=result["residual"])
        s.execution.owned_inventory_ranges.pop(c["con_id"], None)
        self.finished_legs.update({leg["id"]: c["label"] for leg in legs})
        self.known_exec_ids.update(f["exec_id"] for f in fills)
        if status == "RESIDUAL":
            # The block is released: the hedger and RiskGate now treat it as ordinary inventory.
            s.pending_target = None
            s.force_optimize = True
            try:
                position = s.broker.snapshot().quantities().get(c["con_id"], 0) - c["original_quantity"]
                shown = f"the broker position shows {position:+d} against the cycle start"
            except Exception:  # e.g. disconnected: the alert must still go out, once
                position, shown = None, "the broker position is unavailable right now"
            s.alert(
                "error" if result["residual"] or position != 0 else "warning",
                f"Closing burner cycle {c['cycle_id'][:8]} ended RESIDUAL: {reason}; "
                f"{result['residual']} {c['label']} left as ordinary inventory by the recorded fills "
                f"({shown})",
                key=("burner_residual", c["session"], c["cycle_id"]),
            )
        self.cycle, self.leg_ids, self.mismatch = None, set(), None
        self.reported.clear()
        self.reconcile_requested.clear()
        self.cancel_requested.clear()
        self.cancel_failed.clear()
        self.verified.clear()
        self.confirmations.clear()
        self._stats = None
        self._exit_refusal = self.exit_wait_since = None
        self._set(status, reason)
        self._load_open_cycle()

    # ------------------------------------------------------------------ entry decision
    def _wait(self, reason, info=None, code=None):
        self._set("WAITING", reason)
        if info is not None:
            self._record_decision(info, code=code)

    def _record_decision(self, info, force=False, code=None):
        # Throttle on a stable code: reasons that quote live numbers would otherwise change every step.
        candidate = info.get("candidate") or {}
        key = (self.state_name, code or self.reason, candidate.get("con_id"), candidate.get("quantity"))
        if (
            not force
            and key == self._decision_key
            and time.monotonic() - self._decision_at < DECISION_RECORD_SECONDS
        ):
            return
        self._decision_key, self._decision_at = key, time.monotonic()
        self.store.record(
            "burn_decision", self.account, {**info, "state": self.state_name, "reason": self.reason}
        )

    def _blockers(self, snapshot, now):
        s, ex = self.service, self.service.execution
        preview_cooling = (
            hasattr(s.broker, "margin_preview")
            and ex.last_preview is not None
            and not ex.preview_valid
            and now - ex.last_preview < timedelta(minutes=1)
        )
        checks = [
            (ex.paused or ex.pause_requested, "Execution paused"),
            (s.broker.readonly, "Broker read-only"),
            (s.data_wait is not None, "Waiting for verified data"),
            (bool(ex.unresolved()), "Unresolved order submission"),
            (
                bool(snapshot.orders or self.store.intents(self.account, True)),
                "Working orders or intents open",
            ),
            # A recovery context without a target is a leftover of a dropped plan, not a plan.
            (s.pending_target is not None, "Hedge plan pending"),
            (s.last_risk is None or not s.last_risk.passed, "Current risk check not passed"),
            (s.decision_lock.locked(), "Hedge optimization in progress"),
            (ex.burn_open(), "A burn cycle without a final result is still open"),
            (not getattr(s.broker, "reconciled", True), "Broker reconciliation in progress"),
            (preview_cooling, "Credit preview failed within the last minute"),
        ]
        return [reason for blocked, reason in checks if blocked]

    def _path_risk(self, snapshot, quotes, scenarios, quantities, candidate, now):
        """Endpoint and midpoint temporary holdings must pass RiskGate with the cycle reserve."""
        for holding in sorted(
            {candidate.original + candidate.quantity, candidate.original + candidate.quantity // 2}
            - {candidate.original}
        ):
            result = self.service.gate.validate(
                snapshot,
                quotes,
                scenarios,
                {**quantities, candidate.con_id: holding},
                now,
                costs=2 * candidate.expected_loss,
            )
            if not result.passed:
                return "; ".join(result.reasons)
        return None

    async def _evaluate(self, now):
        s, m, p = self.service, self.manifest, self.policy
        mode = self.effective_mode()
        info = {"at": now.isoformat(), "mode": mode}
        if mode == "off":
            self._set(
                "OFF",
                "Burner off for this session (approved event caution)"
                if self.event_off_now()
                else "Burner disabled" + (" by operator" if self.override else " in configuration"),
            )
            return
        day = m.active_session(now)
        if day is None or day not in m.scoring_sessions:
            self._set(
                "IDLE",
                "Day 0 is unscored; the burner never acts" if day == m.day_zero else "Not a scored session",
            )
            return
        if self.active_day != day:
            self.active, self.active_day = False, day
        w = self.window(day)
        info.update(session=str(day), window={k: _iso(v) for k, v in w.items()})
        if now < w["start"]:
            self._set("IDLE", f"Closing window opens at {_et(w['start'])}")
            return
        if now >= w["stock_entry_cutoff"]:
            self._set("DONE", "Entry cutoff passed; any remaining surplus is retained")
            return
        snapshot, quotes = s.broker.snapshot(), s.broker.quotes()
        try:
            target = s.closing_target(snapshot, now)
        except ValueError as exc:
            return self._wait(f"Closing target unavailable: {exc}", info, code="target")
        try:
            estimate = s._estimate_nav(snapshot, quotes)
        except ValueError as exc:
            return self._wait(f"NAV estimate unavailable: {exc}", info, code="nav")
        source = getattr(getattr(s, "nav_tracker", None), "source", None)
        info["nav_source"] = source
        if p.require_pnl_stream and source != NAV_STREAM:
            # Fill attribution re-anchors on every NetLiquidation push, including one sent mid-fill
            # (up to ~$1,000 off on Oct 22): never spend on it.
            return self._wait(
                f"Waiting for the settled 1-second NAV (IBKR P&L stream); now {source}", info, code="nav"
            )
        scenarios = s.last_scenarios
        if (
            scenarios is None
            or s.last_scenario_at is None
            or s.last_scenario_revision != s.broker_event_revision
            or not 0 <= (now - s.last_scenario_at).total_seconds() <= SCENARIO_AGE_SECONDS
            # Built before an event-caution approval changed: rebuild with the approved factors.
            or not s.event_risk.matches(scenarios)
        ):
            self._request_scenarios()
            return self._wait("Waiting for a scenario forecast that includes the latest fills", info)
        if not set(scenarios.ids).issubset(quotes):
            return self._wait("Quote coverage incomplete for the scenario universe", info)
        quantities = snapshot.quantities()
        z = closing_gaps(
            m, scenarios, quotes, quantities, snapshot.cash, snapshot.nav, estimate, target, p.bias
        )
        stats = gap_stats(z, scenarios.weights)
        level = weighted_quantile(z, scenarios.weights, p.spend_quantile)
        needed = max(0.0, level)
        self.active = hysteresis(needed, self.active, p.start_threshold, p.stop_threshold)
        today = self.today(day)
        # Commission reports arrive late: count an estimate for the ones still missing.
        spent = today["realized_cost"] + today["fees_estimate_pending"]
        budget = self._budget(day)
        room = needed if budget is None else max(0.0, min(needed, budget - spent))
        self.last = info  # the latest full gap evaluation stays visible while later steps wait
        info.update(
            target=float(target),
            nav_estimate=estimate,
            gap=stats,
            spend_quantile=p.spend_quantile,
            spend_level=level,
            needed=needed,
            spent=spent,
            budget=budget,
            room=room,
            plan_cap=plan_cap(room, p.stop_threshold),
            engaged=self.active,
            scenario_id=scenarios.identifier,
        )
        quantile = "Median" if p.spend_quantile == 0.5 else f"{p.spend_quantile:.0%} quantile"
        if not self.active:
            self._set(
                "MONITORING",
                f"{quantile} closing gap ${level:,.2f} is within the "
                f"${p.stop_threshold:,.0f}/${p.start_threshold:,.0f} hysteresis band",
            )
            self._record_decision(info, code="hysteresis")
            return
        if budget is not None and budget - spent <= p.stop_threshold:
            return self._wait(
                f"Spend budget used: ${spent:,.2f} of ${budget:,.2f} (it only ever ratchets down)",
                info,
                code="budget",
            )
        settled_at = max(
            (timestamp(r["finished_at"]) for r in today["results"] if r.get("entry_filled")), default=None
        )
        if settled_at is not None and (now - settled_at).total_seconds() < p.settle_seconds:
            return self._wait(
                "Letting the last cycle's fills and commissions reach the NAV stream", info, code="settle"
            )
        if today["cycles"] >= p.max_cycles_per_day:
            return self._wait("Daily cycle limit reached", info)
        expense_remaining = float(p.daily_expense_cap) - spent if p.daily_expense_cap is not None else None
        if expense_remaining is not None and expense_remaining <= 0:
            return self._wait("Daily expense cap reached", info)
        options, rejected = candidates(
            m,
            scenarios,
            quotes,
            quantities,
            now,
            demo=s.gate.demo,
            options_open=now < w["option_entry_cutoff"],
        )
        chosen, improvement, more = select(
            options,
            z,
            scenarios.weights,
            room,
            p,
            expense_remaining=expense_remaining,
            risk_error=lambda c: self._path_risk(snapshot, quotes, scenarios, quantities, c, now),
        )
        rejected.update(more)
        info["rejected"] = dict(list(rejected.items())[:20])
        if chosen is None:
            top = Counter(rejected.values()).most_common(1)
            return self._wait(
                "No eligible round trip: " + (top[0][0] if top else "no candidates"), info, code="none"
            )
        pace = chosen
        if chosen.kind == "OPT":
            # An option clip is large and comes early (options stop at close - 17 min): release it
            # only when the stock window alone could not absorb the need. Pace on the stocks.
            stocks = [o for o in options if o.kind == "STK" and o.expected_loss <= info["plan_cap"]]
            pace = max(stocks, key=lambda o: o.expected_loss, default=chosen)
        cutoff = w["option_entry_cutoff"] if pace.kind == "OPT" else w["stock_entry_cutoff"]
        remaining = (cutoff - now).total_seconds() / 60
        seconds = cycle_seconds(p.assumed_cycle_seconds, today["cycle_seconds"])
        efficiency = fill_efficiency(today["results"])
        capacity = capacity_per_minute(pace.expected_loss, seconds) * efficiency
        waiting = jit_waiting(room, capacity, remaining, p.jit_headroom, p.jit_reserve_minutes)
        info.update(
            candidate=chosen.as_dict(),
            improvement=improvement,
            jit={
                "capacity_per_minute": capacity,
                "remaining_minutes": remaining,
                "cycle_seconds": seconds,
                "fill_efficiency": efficiency,
                "paced_on": f"{pace.label}:{pace.quantity}",
                "waiting": waiting,
            },
        )
        if waiting:
            return self._wait(
                f"Just in time: ${capacity:,.2f}/min over {remaining:.1f} min covers the "
                f"${room:,.2f} still to spend; burning later",
                info,
                code="jit",
            )
        label = f"{chosen.quantity} {chosen.label} (modeled ${chosen.expected_loss:,.2f})"
        if mode == "shadow":
            # Shadow plans no cycles, so it lowers the budget at most once per assumed cycle, as a
            # paper burner would, rather than to the minimum of every one-second sample.
            if time.monotonic() - self._shadow_planned_at >= seconds:
                self._shadow_planned_at = time.monotonic()
                info["budget"] = self._lower_budget(day, spent + needed)
            self._set("SHADOW", "Would burn " + label)
            self._record_decision({**info, "would_trade": True}, code="shadow")
            return
        blockers = self._blockers(snapshot, now)
        if blockers:
            info["blockers"] = blockers
            if s.execution.burn_open():
                self.loaded = False  # adopt the open cycle on the next step
            return self._wait(blockers[0], info)
        # Planning a cycle lowers the budget to what is spent plus what is needed now (the room
        # used above already equals the lowered budget's remainder).
        info["budget"] = self._lower_budget(day, spent + needed)
        await self._open(now, day, w, chosen, improvement, info, snapshot, quotes, scenarios, label)

    def _budget(self, day):
        """Today's spend budget; after a restart, the lowest one today's planned cycles recorded."""
        if day not in self.budgets:
            recorded = [
                c["decision"]["budget"]
                for c in self.store.records("burn_cycle", self.account, 3 * self.policy.max_cycles_per_day)
                if c.get("session") == str(day) and (c.get("decision") or {}).get("budget") is not None
            ]
            self.budgets[day] = min(recorded, default=None)
        return self.budgets[day]

    def _lower_budget(self, day, total):
        budget = self._budget(day)
        self.budgets[day] = total if budget is None else min(budget, total)
        return self.budgets[day]

    async def _open(self, now, day, w, chosen, improvement, info, snapshot, quotes, scenarios, label):
        s, p = self.service, self.policy
        cycle_id = uuid4().hex
        quote = quotes[chosen.con_id]
        price_cap = s.execution.burn_limit(chosen.con_id, quote, 1)
        deadline = w["option_exit_deadline"] if chosen.kind == "OPT" else w["stock_exit_deadline"]
        entry_cutoff = w["option_entry_cutoff"] if chosen.kind == "OPT" else w["stock_entry_cutoff"]
        record = {
            "cycle_id": cycle_id,
            "session": str(day),
            "con_id": chosen.con_id,
            "label": chosen.label,
            "kind": chosen.kind,
            "multiplier": chosen.multiplier,
            "original_quantity": chosen.original,
            "quantity": chosen.quantity,
            "expected_loss": chosen.expected_loss,
            "l1_improvement": improvement,
            "planned_at": now.isoformat(),
            "exit_deadline": deadline.isoformat(),
            "entry_limit": str(price_cap),
            "decision": {
                k: info.get(k)
                for k in (
                    "nav_estimate",
                    "nav_source",
                    "target",
                    "gap",
                    "spend_quantile",
                    "spend_level",
                    "needed",
                    "spent",
                    "budget",
                    "room",
                    "plan_cap",
                    "jit",
                    "mode",
                )
            },
            "scenario_id": scenarios.identifier,
            "scenario_at": _iso(s.last_scenario_at),
            "quote": quote.model_dump(mode="json"),
            "reference_midpoints": {str(chosen.con_id): quote.mid},
            "config_hash": self.manifest.config_hash,
            "admissibility_source": p.admissibility_source,
        }
        # Durable plan first: from here on burn_open() blocks the hedger until a final result.
        self.store.record("burn_cycle", self.account, record, cycle_id)
        self.cycle, self.leg_ids, self.mismatch = record, set(), None
        self.burn_epoch += 1
        self._event(record, "PLANNED", quantity=chosen.quantity, expected_loss=chosen.expected_loss)
        self._set("ENTRY", "Entry " + label)
        self._record_decision({**info, "would_trade": True, "cycle_id": cycle_id}, force=True)
        async with s.reconcile_lock:
            result = await s.execution.submit_burn_leg(
                cycle_id=cycle_id,
                leg="entry",
                attempt=0,
                con_id=chosen.con_id,
                signed=chosen.quantity,
                original_quantity=chosen.original,
                decision_id=cycle_id,
                snapshot=snapshot,
                quotes=quotes,
                scenarios=scenarios,
                now=now,
                quote_age=p.quote_age_seconds,
                price_cap=price_cap,
                reserve=chosen.expected_loss,
                deadline=entry_cutoff,  # re-checked at transmission, after any lock waits
            )
        if result.get("intent_id"):
            self.leg_ids.add(result["intent_id"])
        if result["submitted"]:
            self._event(record, "ENTRY_SUBMITTED", intent_id=result["intent_id"], limit=result["limit"])
        elif result.get("uncertain"):
            self._event(record, "ENTRY_UNCERTAIN", intent_id=result["intent_id"], reason=result["reason"])
            self._set("HALTED", result["reason"])
        else:
            self._event(record, "ENTRY_REFUSED", reason=result["reason"])
            legs = self.store.burn_legs(self.account, cycle_id)
            self._finalize("CLOSED", "Entry not transmitted: " + result["reason"], legs, [], now)

    # ------------------------------------------------------------------ reporting
    def today(self, day):
        if self._stats and self._stats[0] == day and time.monotonic() - self._stats[1] < 5:
            return self._stats[2]
        results = [
            r
            for r in self.store.records(
                "burn_cycle_result", self.account, max(500, 3 * self.policy.max_cycles_per_day)
            )
            if r.get("session") == str(day)
        ]
        results = self._refresh_fees(results)
        transmitted = [r for r in results if r.get("transmitted")]
        measured = [
            r["cycle_seconds"]
            for r in transmitted
            if r["status"] == "CLOSED" and r.get("entry_filled") and r.get("cycle_seconds") is not None
        ]
        stats = {
            "cycles": len(transmitted),
            "closed": sum(r["status"] == "CLOSED" for r in transmitted),
            "residual": sum(r["status"] == "RESIDUAL" for r in results),
            "realized_cost": sum(r["realized_loss"] for r in results),
            "fees": sum(r["fees"] for r in results),
            "fees_pending": sum(bool(r.get("fees_pending")) for r in results),
            "fees_estimate_pending": sum(self._missing_fee_estimate(r) for r in results),
            "turnover": sum(r["turnover"] for r in results),
            "cycle_seconds": measured,
            "mean_cycle_seconds": sum(measured) / len(measured) if measured else None,
            "results": results,
        }
        self._stats = (day, time.monotonic(), stats)
        return stats

    def _missing_fee_estimate(self, result):
        if not result.get("fees_pending"):
            return 0.0
        p = self.manifest.policy  # commission rates live in the strategy policy
        estimate = 0.0
        for side in ("entry", "exit"):
            units, price = result.get(f"{side}_filled") or 0, result.get(f"{side}_vwap") or 0.0
            if units:
                estimate += (
                    option_commission(p, units)
                    if result.get("kind") == "OPT"
                    else stock_commission(p, units, price)
                )
        return max(0.0, estimate - (result.get("fees") or 0.0))

    def _refresh_fees(self, results):
        """Commission reports arrive after executions; refresh fees for cycles still pending."""
        pending = [r for r in results if r.get("fees_pending")]
        if not pending:
            return results
        refs = {"hedge:" + i for r in pending for i in r.get("intent_ids", [])}
        fees = {}
        complete = {}
        for f in self.store.fills_for_refs(self.account, refs):
            key = f["order_ref"]
            fees[key] = fees.get(key, 0.0) + float(f["commission"] or 0)
            complete[key] = complete.get(key, True) and f["commission"] is not None
        refreshed = []
        for r in results:
            if r.get("fees_pending"):
                keys = ["hedge:" + i for i in r.get("intent_ids", [])]
                fee = sum(fees.get(k, 0.0) for k in keys)
                r = {
                    **r,
                    "realized_loss": r["realized_loss"] - r["fees"] + fee,
                    "fees": fee,
                    "fees_pending": not all(complete.get(k, True) for k in keys),
                }
            refreshed.append(r)
        return refreshed

    def export_rows(self):
        """Every session's cycle results (the full audit trail, not only today's)."""
        results = self._refresh_fees(self.store.records("burn_cycle_result", self.account, 1_000_000))
        return [{k: v for k, v in r.items() if k != "intent_ids"} for r in results]

    def state(self):
        now = self.service.now()
        day = self.manifest.active_session(now)
        window = None
        today = None
        if day is not None and day in self.manifest.scoring_sessions:
            w = self.window(day)
            window = {k: _iso(v) for k, v in w.items()}
            today = self.today(day)
        active = (
            self.effective_mode() != "off"
            and window is not None
            and datetime.fromisoformat(window["start"])
            <= now
            < datetime.fromisoformat(window["stock_entry_cutoff"])
        )
        last = self.last or {}
        cycle = None
        if self.cycle is not None:
            c = self.cycle
            cycle = {
                "cycle_id": c["cycle_id"],
                "label": c["label"],
                "kind": c["kind"],
                "quantity": c["quantity"],
                "original_quantity": c["original_quantity"],
                "expected_loss": c["expected_loss"],
                "planned_at": c["planned_at"],
                "exit_deadline": c["exit_deadline"],
                "legs": sorted(self.leg_ids),
            }
        return {
            "mode": self.policy.mode,
            "override": self.override,
            "configured_mode": self.configured_mode(),
            "effective_mode": self.effective_mode(),
            "event_off": self.event_off_now(),
            "active": active,
            "state": self.state_name,
            "reason": self.reason,
            "window": window,
            "symbols": list(self.policy.symbols),
            "option_burn": self.policy.option_burn,
            "admissibility_source": self.policy.admissibility_source,
            "bias": self.policy.bias,
            "bias_source": self.policy.bias_source,
            "thresholds": {"start": self.policy.start_threshold, "stop": self.policy.stop_threshold},
            "target": last.get("target"),
            "nav_estimate": last.get("nav_estimate"),
            "gap": last.get("gap"),
            "needed": last.get("needed"),
            "spend_quantile": self.policy.spend_quantile,
            "spend_level": last.get("spend_level"),
            "spent": last.get("spent"),
            "budget": last.get("budget"),
            "room": last.get("room"),
            "nav_source": last.get("nav_source"),
            "plan_cap": last.get("plan_cap"),
            "engaged": self.active,
            "jit": last.get("jit"),
            "candidate": last.get("candidate"),
            "evaluated_at": last.get("at"),
            "cycle": cycle,
            "today": (
                {k: v for k, v in today.items() if k not in ("results", "cycle_seconds")} if today else None
            ),
            "last_cycles": [
                {k: v for k, v in r.items() if k != "intent_ids"}
                for r in (today["results"][:20] if today else [])
            ],
        }
