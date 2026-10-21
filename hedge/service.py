from __future__ import annotations

import asyncio
import fcntl
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .broker import IBKRBroker, SimBroker
from .burner_service import ClosingBurner
from .closing import CloseMarkRecorder, ClosingRecorder
from .config import NY
from .continuity import DataUnavailable, data_only, quote_coverage
from .data import QuoteRecorder
from .demo import demo_quotes
from .execution import ExecutionManager
from .execution_quality import execution_quality, timestamp
from .models import digest, utcnow
from .moomoo_data import MoomooData, missing_codes
from .optimizer import HedgeOptimizer, execution_cost
from .recovery import RecoveryController, recovery_budget_errors
from .risk import RiskGate
from .scenarios import ScenarioEngine
from .scoring import ScoreLedger
from .setup import SetupPlanner
from .storage import payload
from .supervisor import PROFILES, Supervisor
from .universe import screen_snapshot
from .valuation import NAVTracker

STATE_ROWS = 250  # most recent orders, fills and execution-quality rows in state()


class TradingService:
    def __init__(self, manifest, store, demo=False, history=None, rehearsal=False):
        if rehearsal and not demo:
            raise ValueError("Empty-account rehearsal must use the synthetic broker")
        self.rehearsal = rehearsal
        self.manifest, self.store, self.demo = manifest, store, demo
        self.events = asyncio.Queue()
        self.demo_start = datetime.combine(
            manifest.day_zero if rehearsal else manifest.scoring_sessions[0], datetime.min.time(), NY
        ).replace(hour=10)
        self.started = time.monotonic()
        if demo:
            self.store.clock = self.now
        self.broker = (
            SimBroker(manifest, self.events, self.demo_start, start_empty=rehearsal)
            if demo
            else IBKRBroker(manifest, self.events)
        )
        self.gate = RiskGate(manifest, demo)
        self.execution = ExecutionManager(manifest, store, self.broker, self.gate)
        self.scenarios = ScenarioEngine(manifest, history)
        self.execution.scenario_builder = self.scenarios.build
        self.execution.clock = lambda: self.now()
        self.optimizer = HedgeOptimizer(manifest)
        self.ledger = ScoreLedger(store, manifest)
        self.recorder = QuoteRecorder()
        self.nav_tracker = NAVTracker(manifest)
        self.supervisor = Supervisor()
        self.moomoo_data = MoomooData(manifest) if not demo else None
        if self.moomoo_data and manifest.market_data_provider == "moomoo":
            self.broker.external_quotes = self.moomoo_data.quotes
        self.mode = "PAUSED"
        self.profile = "STANDARD"
        self.ready = False
        self.health = "Starting"
        self.control_epoch = 0
        self.data_wait = None
        self.resume_next_at = 0.0
        self.pause_reason = None
        self.last_snapshot = None
        self.last_quotes = {}
        self.last_risk = None
        self.last_target = None
        self.last_scenarios = None
        self.broker_event_revision = 0
        self.last_scenario_at = None
        self.last_scenario_snapshot_id = None
        self.last_scenario_revision = None
        self.last_optimization = {"STK": 0.0, "OPT": 0.0}
        self.last_trade = {"STK": 0.0, "OPT": 0.0}
        self.last_reconcile = 0.0
        self.tasks = []
        self.market_data_process = None
        self.market_data_log = None
        self.decision_lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.reconcile_lock = asyncio.Lock()
        self.writer_file = None
        self.pg_lock = None
        self.force_optimize = False
        self.pending_target = None
        self.last_decision_id = None
        self.active_plan_started = False
        self.setup_armed = False
        self.setup_target_quantities = None
        self.setup_pairs = {}
        self.setup_decision_id = None
        self.auto_hedge_after_setup = False
        self.autostart_override = None  # dashboard choice; None = configuration
        self.autostart_pending = False
        self.execution.score_context = lambda snapshot, qs, now: (
            self.closing_target(snapshot, now),
            self.nav_tracker.estimate(snapshot, qs, self.manifest.contract_map),
        )
        self.setup_planner = SetupPlanner(manifest, self.gate)
        self.recovery = RecoveryController(manifest, store)
        self.pending_recovery = None
        self.market_data_errors = {}
        self.api_close_warned = set()
        self.closing_recorder = ClosingRecorder(
            manifest, self.ledger, self.broker, self.now, self.alert, demo
        )
        self.close_marks = CloseMarkRecorder(manifest, store, self.now, self._close_marks, self.alert)
        # The closing burner shares this single execution owner; it can wake the monitor early
        # when it needs a scenario forecast that includes its own latest fills.
        self.monitor_wakeup = asyncio.Event()
        self.burner = ClosingBurner(self)

    def now(self):
        return self.demo_start + timedelta(seconds=time.monotonic() - self.started) if self.demo else utcnow()

    async def start(self):
        self.store.initialize()
        Path("data").mkdir(exist_ok=True)
        path = Path("data") / ("writer-" + digest(self.manifest.account)[:12] + ".lock")
        self.writer_file = path.open("a+")
        try:
            fcntl.flock(self.writer_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.writer_file.close()
            raise RuntimeError("Another execution owner already holds this account") from exc
        if self.store.engine.dialect.name == "postgresql":
            from sqlalchemy import text

            self.pg_lock = self.store.engine.connect()
            key = int(digest(self.manifest.account)[:15], 16)
            if not self.pg_lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar():
                self.pg_lock.close()
                raise RuntimeError("Another PostgreSQL execution owner already holds this account")
        self.store.record("configuration", self.manifest.account, self.manifest, self.manifest.config_hash)
        self.burner.restore_setting()
        self.restore_autostart()
        if not self.demo and Path("config/local.moomoo-codes.json").exists():
            self.market_data_log = Path("data/moomoo-collector.log").open("a")
            self.market_data_process = await asyncio.create_subprocess_exec(
                str(Path(".runtime/moomoo-env/bin/python").absolute()),
                "ops/record_moomoo.py",
                stdout=self.market_data_log,
                stderr=self.market_data_log,
                env={k: v for k, v in os.environ.items() if k in ("HOME", "PATH", "TMPDIR", "LANG")},
            )
        if not self.demo and self.manifest.market_data_provider == "moomoo":
            # The collector subscribes only listed codes; anything missing silently has no quotes.
            if missing := missing_codes(self.manifest):
                self.alert(
                    "error" if not Path("config/local.moomoo-codes.json").exists() else "warning",
                    f"Moomoo collector is not subscribed to {len(missing)} manifest contracts "
                    f"(e.g. {', '.join(missing[:4])}); run `python -m hedge.cli moomoo-codes --config "
                    "<config>` and restart",
                )
        if self.demo:
            if not self.rehearsal:
                self.ledger.import_close(
                    self.manifest.day_zero,
                    self.manifest.initial_nav,
                    "SYNTHETIC demo baseline",
                    self.manifest.account,
                )
            quotes = demo_quotes(self.manifest, self.now())
            self.broker.update_quotes(quotes, self.now())
            self.broker.cash -= sum(
                Decimal(str(quotes[i].mid)) * q * self.manifest.contract_map[i].multiplier
                for i, q in self.broker.positions.items()
            )
        self.tasks = [
            asyncio.create_task(self._event_loop()),
            asyncio.create_task(self._monitor_loop()),
            asyncio.create_task(self._supervisor_loop()),
            asyncio.create_task(self.closing_recorder.run()),
            asyncio.create_task(self.close_marks.run()),
            asyncio.create_task(self._burner_loop()),
        ]

    async def _burner_loop(self):
        """Event-driven (1 s fallback) burner steps; a burner fault never hard-stops hedging."""
        while True:
            try:
                await self.burner.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.burner._set("ERROR", f"{type(exc).__name__}: {exc}")
                try:
                    self.alert("error", f"Closing burner step failed: {type(exc).__name__}: {exc}")
                except Exception:
                    pass
            await self.burner.wait(1.0)

    async def stop(self):
        await self.execution.pause()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.market_data_process and self.market_data_process.returncode is None:
            self.market_data_process.terminate()
            await self.market_data_process.wait()
        if self.market_data_log:
            self.market_data_log.close()
        self.recorder.flush()
        await self.broker.disconnect()
        if self.pg_lock:
            self.pg_lock.close()
        if self.writer_file:
            fcntl.flock(self.writer_file, fcntl.LOCK_UN)
            self.writer_file.close()

    async def _event_loop(self):
        while True:
            event, value = await self.events.get()
            try:
                if event in (
                    "position",
                    "execution",
                    "commission",
                    "order",
                    "package_execution",
                    "package_commission",
                    "disconnected",
                    "unsupported_execution",
                ):
                    self.broker_event_revision += 1
                self.store.record(
                    "broker_event", self.manifest.account, {"event": event, "value": payload(value)}
                )
                if event == "execution":
                    fresh = self.store.execution(value)
                    if fresh:
                        self.execution.on_execution(value)
                        self.nav_tracker.execution(value)
                        if not value.order_ref.startswith(("hedge:", "hedge-package:")):
                            self.force_optimize = True
                            self.pending_target = None
                            self.active_plan_started = False
                elif event == "commission":
                    self.store.fee(
                        self.manifest.account, value["exec_id"], value["amount"], value["currency"]
                    )
                    self.nav_tracker.fee(value["exec_id"], value["amount"])
                elif event in ("package_execution", "package_commission"):
                    self.store.record(event, self.manifest.account, value)
                elif event == "order":
                    self.execution.on_order(value)
                elif event in ("disconnected", "unsupported_execution"):
                    if self.last_snapshot:
                        self.last_snapshot = self.last_snapshot.model_copy(
                            update={"connected": False, "reconciled": False}
                        )
                    self.ready = False
                    if event == "disconnected" and self._enter_data_wait("IBKR connection interrupted"):
                        pass  # Cancel/reconcile in the monitor, never block event delivery.
                    else:
                        self._hard_stop(event)
                    self.alert("error", event)
                elif event == "position":
                    identifier, quantity = value["con_id"], int(float(value["quantity"]))
                    previous = (
                        self.last_snapshot.quantities().get(identifier, 0) if self.last_snapshot else None
                    )
                    bounds = self.execution.owned_inventory_ranges.get(identifier)
                    if quantity != previous and not (bounds and bounds[0] <= quantity <= bounds[1]):
                        self.pending_target = None
                        self.active_plan_started = False
                        self.force_optimize = True
                elif event == "broker_error" and value.get("code") in (201, 202):
                    rows = [
                        r
                        for r in self.store.intents(self.manifest.account, True)
                        if r["broker_order_id"] == value["order_id"]
                    ]
                    for row in rows:
                        if row["state"] == "SUBMITTING":
                            self.store.transition(row["id"], "UNKNOWN")
                        self.execution.on_order(
                            {
                                "order_ref": ("hedge-package:" + row["package_id"])
                                if row.get("package_id")
                                else "hedge:" + row["id"],
                                "order_id": value["order_id"],
                                "status": "Inactive" if value["code"] == 201 else "Cancelled",
                            }
                        )
                    # An unfilled IOC burn leg is routinely cancelled (202); never alert per order.
                    # Its Cancelled status usually arrives first, so look the leg up in any state.
                    owner = (
                        rows[0]
                        if rows
                        else self.store.intent_by_order(value["order_id"], self.manifest.account)
                    )
                    if not (value["code"] == 202 and owner and owner.get("purpose") == "burn"):
                        self.alert("warning", value["message"])
                elif event == "broker_error" and value.get("code") in (354, 10090, 10167, 10168):
                    self.market_data_errors[value.get("con_id") or value["order_id"]] = value["message"]
                    self.ready = False
                    self.alert("error", "Live API market data unavailable: " + value["message"])
                if event in ("order", "execution", "commission", "position"):
                    self.burner.on_broker_event(event, value)
            except Exception as exc:
                self._hard_stop(f"Durable event processing failed: {type(exc).__name__}: {exc}")
            finally:
                self.events.task_done()

    def alert(self, severity, message, key=None):
        if key is not None:
            # Once-only alert: a deterministic record id keeps it single across restarts.
            identifier = digest((self.manifest.contest_id, *key))[:32]
            self.store.record(
                "alert", self.manifest.account, {"severity": severity, "message": message}, identifier
            )
            return
        # A retry loop repeats the same few messages: skip one already raised in the last 15 min.
        cutoff = self.store.now() - timedelta(minutes=15)
        recent = self.store.records("alert", self.manifest.account, 10)
        if not any(r.get("message") == message and timestamp(r["at"]) >= cutoff for r in recent):
            self.store.record("alert", self.manifest.account, {"severity": severity, "message": message})

    def _warn_api_previous_close(self, day, record):
        """Once per session: today's target rests on a provisional API close, not the statement."""
        if record is None or record["provenance"] != "api" or day in self.api_close_warned:
            return
        sessions = (self.manifest.day_zero, *self.manifest.scoring_sessions)
        self.alert(
            "warning",
            "Import yesterday's official IBKR statement NAV before the open: "
            f"{day}'s closing target rests on the API closing observation for "
            f"{sessions[sessions.index(day) - 1]}, which can differ from the statement NAV",
            key=("api_previous_close", str(day)),
        )
        self.api_close_warned.add(day)

    def _scores_with_provisional_warning(self):
        """After the final close, warn once if any scored day still rests on an API capture."""
        summary = self.ledger.summary()
        last = self.manifest.scoring_sessions[-1]
        provisional = [d["date"] for d in summary["days"] if d.get("provenance") == "api"]
        if provisional and self.now() >= self.manifest.session_close(last):
            self.alert(
                "warning",
                "Final contest score is provisional: import IBKR statement NAV for " + ", ".join(provisional),
                key=("api_final", tuple(provisional)),
            )
        return summary

    def _hard_stop(self, reason):
        self.control_epoch += 1
        self.data_wait = None
        self.pause_reason = reason
        self.mode, self.ready, self.health = "PAUSED", False, reason
        self.execution.paused = True
        self.broker.readonly = True
        self.pending_target = self.pending_recovery = None
        self.active_plan_started = False

    def restore_autostart(self):
        """At launch: re-apply the dashboard switch and arm the automatic paper start."""
        for record in self.store.records("autostart_setting", self.manifest.account, 1):
            self.autostart_override = record.get("override")
        self.autostart_pending = self.autostart_mode() == "paper"

    def autostart_mode(self):
        return self.manifest.autostart if self.autostart_override is None else self.autostart_override

    def set_autostart(self, mode):
        """Dashboard switch, persisted across restarts. 'paper' starts paper trading automatically
        now (from PAUSED) and after every launch once all activation checks pass; 'off' cancels a
        pending automatic start but never stops paper trading that is already running."""
        if mode not in ("off", "paper"):
            raise ValueError("Automatic start must be off or paper")
        self.autostart_override = None if mode == self.manifest.autostart else mode
        self.store.record(
            "autostart_setting",
            self.manifest.account,
            {"override": self.autostart_override, "mode": mode, "config_hash": self.manifest.config_hash},
        )
        if mode == "paper":
            self.autostart_pending = self.mode == "PAUSED"
        else:
            self.autostart_pending = False
            if self.mode == "WAITING_DATA" and self.data_wait and self.data_wait.get("autostart"):
                self._hard_stop("Automatic paper start cancelled by operator")
        return self.state()

    def _autostart_step(self, mandate_errors):
        """Launch-time automatic paper start. It only hands over to the automatic-resume path
        (WAITING_DATA -> control('paper', _automatic=True)), so every paper activation check,
        trading-window check and live-data check still applies; any operator control cancels it."""
        if not self.autostart_pending or self.mode != "PAUSED":
            return
        if mandate_errors:
            self.health = "Automatic paper start waits for the mandated holdings: " + "; ".join(
                mandate_errors
            )
            return
        self.autostart_pending = False
        self.data_wait = {
            "resume_mode": "PAPER",
            "reason": "Automatic paper start at launch",
            "since": self.now().isoformat(),
            "config_hash": self.manifest.config_hash,
            "profile": self.profile,
            "epoch": self.control_epoch,
            "autostart": True,
        }
        self.resume_next_at = 0.0
        self.mode, self.ready = "WAITING_DATA", False
        self.execution.paused = True
        self.broker.readonly = True
        self.pending_target = self.pending_recovery = None
        self.health = "Automatic paper start: waiting for the full paper activation checks"
        self.store.record(
            "continuity",
            self.manifest.account,
            {
                **self.data_wait,
                "status": "AUTOSTART",
                "snapshot_id": self.last_snapshot.version if self.last_snapshot else None,
            },
        )

    def _enter_data_wait(self, reason):
        if self.mode in ("PAPER", "SETUP"):
            self.data_wait = {
                "resume_mode": self.mode,
                "reason": reason,
                "since": self.now().isoformat(),
                "config_hash": self.manifest.config_hash,
                "profile": self.profile,
                "epoch": self.control_epoch,
            }
            self.resume_next_at = 0.0
            self.store.record(
                "continuity",
                self.manifest.account,
                {
                    **self.data_wait,
                    "status": "WAITING_DATA",
                    "snapshot_id": self.last_snapshot.version if self.last_snapshot else None,
                },
            )
        elif self.mode != "WAITING_DATA" or self.data_wait is None:
            return False
        self.data_wait["reason"] = reason
        self.mode, self.ready = "WAITING_DATA", False
        self.execution.paused = True
        self.broker.readonly = True
        self.pending_target = self.pending_recovery = None
        self.active_plan_started = False
        self.health = "Waiting for verified data; automatic preflight retry: " + reason
        return True

    async def _wait_for_data(self, reason):
        if self._enter_data_wait(reason) and not self.execution.pause_requested:
            await self.execution.pause()

    async def _resume_after_data_wait(self):
        wait = self.data_wait
        if self.mode != "WAITING_DATA" or wait is None:
            return
        if (
            wait["epoch"] != self.control_epoch
            or wait["config_hash"] != self.manifest.config_hash
            or wait["profile"] != self.profile
        ):
            self._hard_stop("Automatic resume invalidated by operator/configuration change")
            return
        if time.monotonic() < self.resume_next_at:
            return
        if self.last_risk and any(
            reason.startswith(
                (
                    "Non-live quote:",
                    "Stale/future quote:",
                    "Missing/stale option Greeks:",
                    "Future option Greeks:",
                )
            )
            for reason in self.last_risk.reasons
        ):
            return
        if not self.manifest.can_trade(
            self.now(),
            "OPT" if wait["resume_mode"] == "SETUP" else "STK",
            setup=wait["resume_mode"] == "SETUP",
        ):
            self.health = "Automatic resume waiting for a configured trading window"
            return
        self.resume_next_at = time.monotonic() + 15
        if not self.execution.pause_requested:
            await self.execution.pause()
        try:
            await self.control("paper" if wait["resume_mode"] == "PAPER" else "setup", _automatic=True)
        except (ValueError, TimeoutError, ConnectionError) as exc:
            if self.mode == "WAITING_DATA" and self.data_wait is wait:
                self.health = "Automatic resume waiting for full preflight: " + (
                    str(exc) or type(exc).__name__
                )

    def _estimate_nav(self, snapshot, quotes):
        try:
            return self.nav_tracker.estimate(snapshot, quotes, self.manifest.contract_map)
        except ValueError as exc:
            if str(exc).startswith(("NAV attribution waiting", "Fill quote missing")):
                raise DataUnavailable(str(exc)) from None
            raise

    async def reconcile(self):
        async with self.reconcile_lock:
            return await self._reconcile()

    async def _reconcile(self):
        prior = self.last_snapshot.quantities() if self.last_snapshot else None
        snapshot = await asyncio.wait_for(self.broker.reconcile(), 30)
        await self.events.join()
        await self.execution.reconcile(snapshot)
        self.last_snapshot = snapshot
        self.store.snapshot(snapshot)
        self.last_reconcile = time.monotonic()
        if (
            snapshot.quantities() != prior
            or self.execution.unresolved()
            or any(not o.order_ref.startswith(("hedge:", "hedge-package:")) for o in snapshot.orders)
        ):
            self.pending_target = None
            self.active_plan_started = False
            self.force_optimize = True
        return snapshot

    async def _monitor_loop(self):
        next_connection = 0.0
        while True:
            try:
                if not self.broker.snapshot().connected if self.demo else not self.broker.ib.isConnected():
                    if time.monotonic() >= next_connection:
                        await self.broker.connect()
                        await self.reconcile()
                        if self.setup_armed:
                            self.mode = "WAITING_SETUP"
                        if self.mode != "WAITING_DATA":
                            self.health = (
                                "Connected; automatic paper start pending its activation checks"
                                if self.autostart_pending
                                else "Connected; paper execution remains disabled until explicitly enabled"
                            )
                    else:
                        await asyncio.sleep(1)
                        continue
                if self.demo:
                    self.broker.update_quotes(
                        demo_quotes(self.manifest, self.now(), time.monotonic() - self.started), self.now()
                    )
                    if self.mode in ("PAPER", "SETUP"):
                        for order in list(self.broker.orders):
                            await self.broker.fill(order)
                if time.monotonic() - self.last_reconcile >= self.manifest.policy.reconcile_seconds:
                    await self.reconcile()
                self.last_snapshot = self.broker.snapshot()
                if self.manifest.next_close(self.now()) is None:
                    if self.mode in ("PAPER", "SETUP", "WAITING_DATA"):
                        self._hard_stop("Contest ended; filled holdings retained")
                        await self.execution.pause()
                    await asyncio.sleep(self.manifest.policy.risk_seconds)
                    continue
                baseline = self.ledger.closes().get(self.manifest.day_zero)
                if baseline is not None:
                    self.gate.engine.baseline_nav = self.optimizer.baseline_nav = float(baseline)
                self.last_quotes = self.broker.quotes()
                self.recovery.observe_close(self.last_quotes, self.now())
                if (
                    self.moomoo_data
                    and self.moomoo_data.path.exists()
                    and self.manifest.market_data_provider != "moomoo"
                ):
                    self.last_quotes = self.moomoo_data.quotes(self.now())
                for identifier, quote in self.last_quotes.items():
                    if quote.data_type == "live":
                        self.market_data_errors.pop(identifier, None)
                self.store.snapshot(self.last_snapshot)
                self.recorder.append(self.last_quotes, self.manifest.contract_map, self.now())
                missing = set(self.last_snapshot.quantities()) - self.last_quotes.keys()
                if missing:
                    if not missing.issubset(self.manifest.contract_map):
                        raise ValueError(f"Unsupported actual broker positions: {sorted(missing)}")
                    raise DataUnavailable(f"Waiting for quotes on held conIds {sorted(missing)}")
                mandate_errors = self.gate.registry.check(self.last_snapshot.quantities())
                if mandate_errors:
                    self.health = "; ".join(mandate_errors)
                    self.ready = False
                    if self.mode == "PAPER":
                        self._hard_stop("Mandatory holdings mismatch: " + "; ".join(mandate_errors))
                        await self.execution.pause()
                    if self.mode != "SETUP":
                        self.alert("error", self.health)
                self._autostart_step(mandate_errors)
                if self.last_quotes:
                    ids = screen_snapshot(self.manifest, self.last_quotes, self.last_snapshot)
                    scenario_at = self.now()
                    scenario_snapshot_id = self.last_snapshot.version
                    scenario_revision = self.broker_event_revision
                    self.last_scenarios = await asyncio.to_thread(
                        self.scenarios.build, self.last_snapshot, self.last_quotes, scenario_at, ids
                    )
                    self.last_scenario_at = scenario_at
                    self.last_scenario_snapshot_id = scenario_snapshot_id
                    self.last_scenario_revision = scenario_revision
                    self.last_risk = self.gate.validate(
                        self.last_snapshot,
                        self.last_quotes,
                        self.last_scenarios,
                        self.last_snapshot.quantities(),
                        self.now(),
                    )
                    self.ready = (
                        not mandate_errors and self.last_risk.passed and not self.execution.unresolved()
                    )
                    if self.mode in ("PAPER", "SETUP") and data_only(self.last_risk.reasons):
                        await self._wait_for_data("; ".join(self.last_risk.reasons))
                    if self.mode in ("PAPER", "SETUP") and self.last_snapshot.orders:
                        working_snapshot = self.broker.snapshot()
                        projected_check = self.gate.validate(
                            working_snapshot,
                            self.broker.quotes(),
                            self.last_scenarios,
                            working_snapshot.projected(),
                            self.now(),
                            corrective=not self.last_risk.passed,
                            day_zero=self.mode == "SETUP",
                        )
                        if not projected_check.passed:
                            message = "Working-order check: " + "; ".join(projected_check.reasons)
                            if data_only(projected_check.reasons):
                                await self._wait_for_data(message)
                            else:
                                self._hard_stop(message)
                                await self.execution.pause()
                                self.alert("error", message)
                    if self.mode == "WAITING_DATA":
                        self.ready = False
                    await self._resume_after_data_wait()
                    # Service existing orders before a solver can consume its
                    # time budget. Use the just-rebuilt risk scenarios.
                    self.execution.current_scenarios = self.last_scenarios
                    await self.execution.manage(self.broker.quotes(), self.now())
                    if self.mode in ("SHADOW", "PAPER"):
                        # A closing-burner cycle owns its temporary inventory until it is final.
                        if self.optimization_due() and not self.execution.burn_open():
                            await self.optimize()
                    elif self.mode == "SETUP":
                        await self._setup_slice()
                    elif self.mode == "WAITING_SETUP" and self.manifest.can_trade(
                        self.now(), "OPT", setup=True
                    ):
                        if self.now().astimezone(NY).date() == self.manifest.purchase_session:
                            try:
                                await self.control("setup")
                                self.setup_armed = False
                            except ValueError as exc:
                                self.health = f"Scheduled purchases waiting for preflight: {exc}"
                    if self.mode == "SHADOW" and self.auto_hedge_after_setup and not mandate_errors:
                        day = self.manifest.active_session(self.now())
                        if day == self.manifest.day_zero or (
                            day and self.ledger.previous_close(day) is not None
                        ):
                            try:
                                await self.control("paper")
                                self.auto_hedge_after_setup = False
                            except ValueError as exc:
                                self.health = f"Purchases complete; hedge activation waiting: {exc}"
                        else:
                            self.health = (
                                "Purchases complete; automatic hedging awaits previous official closing NAV"
                            )
                if self.mode == "PAPER" and self.pending_target and not self.execution.burn_open():
                    await self._execute_pending()
                self.execution.current_scenarios = self.last_scenarios
                # An optimizer/preflight may take seconds. Never manage a live
                # order using the quote dictionary captured before that work.
                await self.execution.manage(self.broker.quotes(), self.now())
                if self.execution.paused and self.mode in ("PAPER", "SETUP"):
                    self._hard_stop("Execution stopped; broker order state requires reconciliation")
                if self.mode == "PAUSED" and self.execution.pause_requested:
                    self.health = (
                        (self.pause_reason or "Trading paused") + "; all cancellations confirmed"
                        if self.execution.pause_complete()
                        else (self.pause_reason or "Trading paused") + "; awaiting cancellation confirmation"
                    )
                if (self.setup_armed or self.mode == "SETUP") and self.now() >= self.manifest.session_close(
                    self.manifest.purchase_session
                ) - timedelta(minutes=self.manifest.execution.setup_cutoff_minutes):
                    self.setup_armed = self.auto_hedge_after_setup = False
                    self._hard_stop("Scheduled mandatory purchase window ended")
                    await self.execution.pause()
                    self.alert(
                        "error",
                        "Scheduled mandatory purchase window ended; inspect holdings and organizer rules",
                    )
                if self.market_data_errors and not self.last_quotes:
                    self.health = "IBKR connected; live stock/option API subscriptions unavailable; purchases and hedging blocked"
                day = self.manifest.active_session(self.now())
                if self.now() >= self.manifest.session_close(
                    self.manifest.scoring_sessions[-1]
                ) and self.mode in ("PAPER", "SETUP"):
                    self._hard_stop("Contest ended; filled holdings retained")
                    await self.execution.pause()
                if day:
                    record = self.ledger.previous_close_record(day)
                    if record is None and day != self.manifest.day_zero:
                        self.alert(
                            "warning", "Previous official closing NAV missing; score-aware trading blocked"
                        )
                    self._warn_api_previous_close(day, record)
                await self._monitor_pause()
            except asyncio.CancelledError:
                raise
            except (DataUnavailable, TimeoutError, ConnectionError) as exc:
                message = str(exc) or type(exc).__name__
                self.ready = False
                self.pending_target = self.pending_recovery = None
                await self._wait_for_data(message)
                if self.mode != "WAITING_DATA":
                    self.health = "Waiting for broker/data: " + message
                next_connection = time.monotonic() + 15
                await asyncio.sleep(self.manifest.policy.risk_seconds)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if self.data_wait and self.data_wait.get("autostart"):
                    # Paper never started: keep the launch-time automatic start armed.
                    self.autostart_pending = True
                if self.mode in ("PAPER", "SETUP", "WAITING_DATA"):
                    self._hard_stop(message)
                    await self.execution.pause()
                else:
                    self.health, self.ready = message, False
                try:
                    self.alert("error", self.health)
                except Exception:
                    pass
                next_connection = time.monotonic() + 15
                await asyncio.sleep(5)

    async def _monitor_pause(self):
        """Sleep risk_seconds, or less when the closing burner needs a fresh scenario forecast."""
        try:
            await asyncio.wait_for(self.monitor_wakeup.wait(), self.manifest.policy.risk_seconds)
        except TimeoutError:
            pass
        self.monitor_wakeup.clear()

    def optimization_due(self):
        """Continuous evaluation remains independent of whether past trades filled."""
        elapsed = time.monotonic()
        return self.pending_target is None and (
            self.force_optimize
            or elapsed - self.last_optimization["STK"] >= self.manifest.policy.stock_seconds
            or elapsed - self.last_optimization["OPT"] >= self.manifest.policy.option_seconds
        )

    def closing_target(self, snapshot, now):
        """Require the scored session's own baseline before generating a target."""
        day = self.manifest.active_session(now)
        if day is None:
            raise ValueError("Outside the configured contest sessions")
        if day == self.manifest.day_zero:
            return snapshot.nav
        previous = self.ledger.previous_close(day)
        if previous is None:
            sessions = (self.manifest.day_zero, *self.manifest.scoring_sessions)
            previous_day = sessions[sessions.index(day) - 1]
            raise ValueError(
                f"Missing previous closing NAV for {previous_day}; import that IBKR closing NAV "
                "before score-aware paper activation"
            )
        target = self.ledger.target_for(day, previous, allow_estimate=True)
        if target is None:
            raise ValueError("Daily cash benchmark rate or approved forecast required before paper trading")
        return target

    async def optimize(self):
        if self.manifest.scoring_rule == "pending_interest_adjustment":
            self.health = "Scoring revision pending; interest-offset optimization disabled"
            return
        async with self.decision_lock:
            if self.execution.burn_open():
                self.health = "Closing-burner cycle open; hedge optimization waits for its result"
                return
            decision_epoch = self.control_epoch
            burn_epoch = self.burner.burn_epoch
            snap, quotes, scenarios, now = (
                self.last_snapshot,
                dict(self.last_quotes),
                self.last_scenarios,
                self.now(),
            )
            try:
                previous = self.closing_target(snap, now)
            except ValueError as exc:
                self.last_target = None
                self.health = f"Optimization blocked: {exc}"
                return
            mandate_errors = self.gate.registry.check(snap.quantities())
            if mandate_errors:
                self.last_target = None
                self.health = "; ".join(mandate_errors)
                return
            full = (
                self.force_optimize
                or time.monotonic() - self.last_optimization["OPT"] >= self.manifest.policy.option_seconds
            )
            quote_coverage(quotes, scenarios.ids)
            estimate = self._estimate_nav(snap, quotes)
            target = await asyncio.to_thread(
                self.optimizer.solve,
                snap,
                quotes,
                scenarios,
                previous,
                estimate,
                not full,
                PROFILES[self.profile],
            )
            failed_primary = None
            current_risk = self.gate.engine.evaluate(snap, quotes, scenarios, snap.quantities())
            if target.status != "OPTIMAL" and any(
                reason.startswith("Residual delta cap:") for reason in current_risk.reasons
            ):
                # Discard the failed solve entirely. A new, smaller risk solve
                # freezes every option and neutralizes delta through stocks,
                # while retaining all hard combined-portfolio constraints.
                failed_primary = target.status
                refreshed = self.broker.snapshot()
                if refreshed.quantities() != snap.quantities() or refreshed.orders != snap.orders:
                    self.force_optimize = True
                    return
                snap, quotes, now = refreshed, dict(self.broker.quotes()), self.now()
                previous = self.closing_target(snap, now)
                ids = screen_snapshot(self.manifest, quotes, snap)
                scenario_revision = self.broker_event_revision
                scenarios = await asyncio.to_thread(self.scenarios.build, snap, quotes, now, ids)
                self.last_snapshot, self.last_quotes, self.last_scenarios = snap, quotes, scenarios
                self.last_scenario_at = now
                self.last_scenario_snapshot_id = snap.version
                self.last_scenario_revision = scenario_revision
                estimate = self.nav_tracker.estimate(snap, quotes, self.manifest.contract_map)
                target = await asyncio.to_thread(
                    self.optimizer.solve, snap, quotes, scenarios, previous, estimate, True, "delta"
                )
                target = target.model_copy(
                    update={
                        "explanation": f"Corrective stock-only delta hedge after rejected solver {failed_primary}"
                    }
                )
            # Broker inventory/order changes during optimization invalidate its decision.
            newest = self.broker.snapshot()
            if (
                newest.quantities() != snap.quantities()
                or newest.orders != snap.orders
                or decision_epoch != self.control_epoch
                # A completed burn round trip restores quantities while NAV and fees moved.
                or burn_epoch != self.burner.burn_epoch
            ):
                self.last_target = None
                self.force_optimize = True
                return
            ordinary_floor = max(
                self.manifest.policy.score_threshold,
                self.manifest.policy.uncertainty_multiplier * target.uncertainty,
            )
            recovery_baseline = target
            if target.current_score - target.expected_score <= ordinary_floor:
                recovery_baseline = self.optimizer.evaluate_target(
                    snap,
                    quotes,
                    scenarios,
                    target.model_copy(update={"quantities": snap.quantities()}),
                    previous,
                    estimate,
                )
            recovery_target, recovery_scenarios, recovery_record = await asyncio.to_thread(
                self.recovery.evaluate,
                self.optimizer,
                snap,
                quotes,
                scenarios,
                recovery_baseline,
                previous,
                estimate,
                now,
            )
            self.store.record("recovery_shadow", snap.account, recovery_record)
            newest = self.broker.snapshot()
            if (
                newest.quantities() != snap.quantities()
                or newest.orders != snap.orders
                or burn_epoch != self.burner.burn_epoch
            ):
                self.force_optimize = True
                return
            recovery_selected = False
            baseline_quantities = dict(recovery_baseline.quantities)
            if (
                self.recovery.mode == "paper"
                and failed_primary is None
                and recovery_target is not None
                and not self.recovery.paper_errors(recovery_record.get("model_hash"))
            ):
                if all(
                    newest.quantities().get(i, 0) == baseline_quantities.get(i, 0)
                    for i in set(newest.quantities()) | set(baseline_quantities)
                ):
                    target, scenarios = recovery_target, recovery_scenarios
                    recovery_selected = True
            gate = self.gate.validate(
                newest,
                self.broker.quotes(),
                scenarios,
                target.quantities,
                self.now(),
                2 * target.estimated_cost,
            )
            if recovery_selected:
                extra = recovery_budget_errors(
                    self.manifest,
                    self.recovery.config,
                    newest,
                    self.broker.quotes(),
                    scenarios,
                    target.quantities,
                    baseline_quantities,
                    2 * target.estimated_cost,
                )
                if extra:
                    gate = gate.model_copy(update={"passed": False, "reasons": (*gate.reasons, *extra)})
            improvement = target.current_score - target.expected_score
            threshold = max(
                self.manifest.policy.score_threshold,
                self.manifest.policy.uncertainty_multiplier * target.uncertainty,
            )
            corrective = not self.gate.engine.evaluate(
                newest, self.broker.quotes(), scenarios, newest.quantities()
            ).passed
            record = {
                **target.model_dump(mode="json"),
                "reference_midpoints": {str(i): q.mid for i, q in quotes.items()},
                "risk_passed": gate.passed,
                "risk_reasons": gate.reasons,
                "risk_metrics": gate.metrics,
                "improvement": improvement,
                "threshold": threshold,
                "trade_eligible": gate.passed
                and target.status == "OPTIMAL"
                and (improvement > threshold or corrective),
                "corrective": corrective,
                "rejected_primary_solver": failed_primary,
                "recovery": recovery_selected,
                "recovery_reference": baseline_quantities if recovery_selected else None,
                "nav_gap": estimate - float(previous),
                "closing_target": float(previous),
                "optimization_scope": "CORRECTIVE_STOCK" if failed_primary else "FULL" if full else "STOCK",
                "hold_distance": target.current_score,
                "proposed_distance": target.expected_score,
                "cost_treatment": "burn_surplus" if self.manifest.policy.burn_surplus else "additive",
                # E|g| + c - E|g - c|: how much of the modeled benefit is spending a surplus on costs.
                "burn_component": target.components.get("burn", 0.0),
                "overnight_score": target.components.get("overnight", 0.0),
                "blocking_reasons": (
                    list(gate.reasons)
                    + ([f"Solver: {target.status}"] if target.status != "OPTIMAL" else [])
                    + (
                        [f"Modeled improvement ${improvement:.2f} does not exceed threshold ${threshold:.2f}"]
                        if improvement <= threshold and gate.passed and not corrective
                        else []
                    )
                ),
                "source": scenarios.source,
                "profile": self.profile,
                "at": now.isoformat(),
            }
            self.last_decision_id = self.store.record("decision", snap.account, record)
            self.last_target = target
            self.last_optimization["STK"] = time.monotonic()
            if full:
                self.last_optimization["OPT"] = time.monotonic()
            self.force_optimize = False
            if self.mode == "PAPER" and record["trade_eligible"]:
                self.pending_target = target
                self.pending_recovery = (
                    {
                        "reference": baseline_quantities,
                        "scenarios": scenarios,
                        "generated_at": now,
                        "model_hash": recovery_record.get("model_hash"),
                    }
                    if recovery_selected
                    else None
                )
                self.active_plan_started = False
            if self.mode in ("SHADOW", "PAPER"):
                self.health = (
                    "Monitoring: proposal eligible"
                    if record["trade_eligible"]
                    else "Monitoring: " + "; ".join(record["blocking_reasons"] or ["No qualifying trade"])
                )

    async def _execute_pending(self):
        if self.execution.burn_open():
            return
        target = self.pending_target
        snapshot = self.broker.snapshot()
        quotes = self.broker.quotes()
        actual = snapshot.quantities()
        scenario_for_plan = self.last_scenarios
        self.execution.recovery_checks = None
        if self.pending_recovery is not None:
            approval_errors = self.recovery.paper_errors(self.pending_recovery["model_hash"])
            if approval_errors:
                await self.execution.pause()
                self.recovery.mode = "shadow"
                self.mode = "SHADOW" if self.execution.pause_complete() else "PAUSED"
                self.pending_target = self.pending_recovery = None
                self.execution.recovery_checks = None
                self.force_optimize = True
                self.health = "Recovery approval invalidated; " + "; ".join(approval_errors)
                return
            if (
                self.recovery.mode != "paper"
                or (self.now() - self.pending_recovery["generated_at"]).total_seconds()
                > self.recovery.config.max_signal_age
            ):
                self.pending_target = self.pending_recovery = None
                self.force_optimize = True
                return
            scenario_for_plan = self.pending_recovery["scenarios"]
            reference = dict(self.pending_recovery["reference"])
            self.execution.recovery_checks = lambda snap, qs, sc, amounts, costs: (
                self.recovery.execution_errors(snap, qs, sc, amounts, reference, costs, self.now())
            )
        if snapshot.orders or self.store.intents(snapshot.account, True):
            return
        changed = [
            i for i in set(actual) | set(target.quantities) if actual.get(i, 0) != target.quantities.get(i, 0)
        ]
        if not changed:
            self.pending_target = None
            self.active_plan_started = False
            return
        corrective = not self.gate.engine.evaluate(snapshot, quotes, scenario_for_plan, actual).passed
        if not self.active_plan_started and not corrective:
            for i in changed:
                kind = self.manifest.contract_map[i].kind
                cooldown = (
                    self.manifest.policy.option_seconds
                    if kind == "OPT"
                    else self.manifest.policy.stock_seconds
                )
                if time.monotonic() - self.last_trade[kind] < cooldown:
                    return
        day = self.manifest.active_session(self.now())
        previous = self.ledger.previous_close(day) if day != self.manifest.day_zero else snapshot.nav
        if previous is None:
            self.pending_target = None
            return
        if day != self.manifest.day_zero:
            previous = self.ledger.target_for(day, previous, allow_estimate=True)
        if previous is None:
            self.pending_target = None
            return
        if not set(scenario_for_plan.ids).issubset(quotes):
            self.pending_target = self.pending_recovery = None
            self.active_plan_started, self.force_optimize = False, True
            self.health = "Quote coverage changed during execution planning; refreshing the hedge proposal"
            return
        estimate = self._estimate_nav(snapshot, quotes)
        target = self.optimizer.evaluate_target(
            snapshot, quotes, scenario_for_plan, target, previous, estimate
        )
        threshold = max(
            self.manifest.policy.score_threshold,
            self.manifest.policy.uncertainty_multiplier * target.uncertainty,
        )
        if target.current_score - target.expected_score <= threshold and not corrective:
            self.pending_target = None
            self.active_plan_started = False
            return
        if not self.gate.validate(
            snapshot, quotes, scenario_for_plan, target.quantities, self.now(), 2 * target.estimated_cost
        ).passed:
            self.pending_target = None
            self.force_optimize = True
            return
        result = await self.execution.submit_slice(
            target, self.last_decision_id, snapshot, quotes, scenario_for_plan, self.now()
        )
        if result["submitted"]:
            # Cooldown begins for a plan, not every slice, to allow completion.
            if not self.active_plan_started:
                for i in changed:
                    self.last_trade[self.manifest.contract_map[i].kind] = time.monotonic()
            self.active_plan_started = True
        else:
            self.health = result["reason"]
            if not self.execution.unresolved():
                self.pending_target = None
                self.active_plan_started = False
                self.force_optimize = True

    def _setup_quantities(self, snapshot, quotes):
        plan = self.setup_planner.plan(snapshot, quotes, self.last_scenarios, self.now())
        self.setup_pairs = plan["pairs"]
        self.setup_target = plan["target"]
        return dict(plan["target"].quantities)

    async def _setup_slice(self):
        snap = self.broker.snapshot()
        if snap.orders or self.store.intents(snap.account, True) or self.execution.burn_open():
            return
        quotes = self.broker.quotes()
        if self.setup_target_quantities is None or not self.setup_pairs:
            raise ValueError("A complete protected setup plan is required before purchasing")
        desired = dict(self.setup_target_quantities)
        actual = snap.quantities()
        # Stock hedges and any existing inventory adjustments are established
        # first. Each subsequent mandate increment arrives with its option hedge.
        prepared = dict(desired)
        for i, h in self.setup_pairs.items():
            prepared[i] = actual.get(i, 0)
            prepared[h] = desired[h] + self.manifest.mandatory[i] - actual.get(i, 0)
        if any(actual.get(i, 0) != q for i, q in prepared.items()):
            target = self.setup_target.model_copy(
                update={
                    "quantities": prepared,
                    "snapshot_id": snap.version,
                    "scenario_id": self.last_scenarios.identifier,
                }
            )
            result = await self.execution.submit_slice(
                target, self.setup_decision_id, snap, quotes, self.last_scenarios, self.now(), setup=True
            )
            self.health = "Initial hedge preparation submitted" if result["submitted"] else result["reason"]
            return
        complete = not self.gate.registry.check(actual)
        if complete:
            risk = self.gate.validate(snap, quotes, self.last_scenarios, actual, self.now(), day_zero=True)
            if not risk.passed:
                raise ValueError("Completed setup risk: " + "; ".join(risk.reasons))
            await self.reconcile()
            actual = self.broker.snapshot().quantities()
            if self.gate.registry.check(actual) or any(
                actual.get(h, 0) > -actual.get(i, 0) for i, h in self.setup_pairs.items()
            ):
                raise ValueError("Protected setup fills did not reconcile")
            self.mode = "SHADOW"
            self.execution.paused = True
            self.broker.readonly = True
            self.force_optimize = True
            self.health = "Mandatory and initial hedge holdings reconciled; protected setup complete"
            self.store.record(
                "setup_complete",
                snap.account,
                {"quantities": actual, "pairs": self.setup_pairs, "at": self.now()},
            )
            return
        candidates = []
        for i, h in self.setup_pairs.items():
            if actual.get(i, 0) >= self.manifest.mandatory[i]:
                continue
            if actual.get(h, 0) > -actual.get(i, 0):
                raise ValueError("Unmatched mandatory package leg; stop and reconcile")
            amounts = {**actual, i: actual.get(i, 0) + 1, h: actual.get(h, 0) - 1}
            costs = execution_cost(self.manifest, quotes, [i, h], [1, -1])
            risk = self.gate.validate(
                snap, quotes, self.last_scenarios, amounts, self.now(), 2 * costs, day_zero=True
            )
            if risk.passed:
                candidates.append((risk.metrics["stress_loss"], i, h))
        if not candidates:
            self.health = "Protected setup waiting: no matched package passes current risk"
            return
        _, i, h = min(candidates)
        result = await self.execution.submit_package(
            i, h, self.setup_decision_id, snap, quotes, self.last_scenarios, self.now()
        )
        self.health = (
            "Guaranteed mandatory + hedge option package submitted"
            if result["submitted"]
            else result["reason"]
        )

    def _close_marks(self, now, day, phase):
        """Everything needed later to split a day's score miss into Greeks, marks and costs."""
        snap, quotes = self.last_snapshot, dict(self.last_quotes)
        if phase == "post_close":
            # The monitor stops refreshing quotes after the final close; read the feed directly.
            quotes = dict(self.broker.quotes()) or quotes
        if snap is None or not quotes:
            return None  # Not connected/quoted yet (e.g. just restarted): retry on the next tick.
        try:
            estimate = self._estimate_nav(snap, quotes) if snap else None
        except ValueError:
            estimate = None
        try:
            target = self.closing_target(snap, now) if snap else None
        except ValueError:
            target = None
        contracts = self.manifest.contract_map
        return {
            "contest": self.manifest.contest_id,
            "date": str(day),
            "phase": phase,
            "at": now.isoformat(),
            "scheduled_close": self.manifest.session_close(day).isoformat(),
            "config_hash": self.manifest.config_hash,
            "nav": str(snap.nav) if snap else None,
            "cash": str(snap.cash) if snap else None,
            "accrued_cash": str(snap.accrued_cash) if snap and snap.accrued_cash is not None else None,
            "account_at": snap.account_at.isoformat() if snap else None,
            "nav_estimate": estimate,
            "closing_target": str(target) if target is not None else None,
            "positions": {str(i): q for i, q in (snap.quantities() if snap else {}).items()},
            "quotes": {
                str(i): {
                    "label": contracts[i].label,
                    "kind": contracts[i].kind,
                    "bid": q.bid,
                    "ask": q.ask,
                    "mid": q.mid,
                    "data_type": q.data_type,
                    "provider": q.provider,
                    "observed_at": q.observed_at.isoformat(),
                    "spot": q.spot,
                    "iv": q.iv,
                    "delta": q.delta,
                    "gamma": q.gamma,
                    "vega": q.vega,
                    "theta": q.theta,
                    "model_price": q.model_price,
                }
                for i, q in quotes.items()
                if i in contracts
            },
            # IBKR's own marks are what the official NAV uses; Moomoo mids are our estimate.
            "ibkr_marks": {
                str(i): mark for i, mark in getattr(self.broker, "portfolio_marks", dict)().items()
            },
            "risk_metrics": self.last_risk.metrics if self.last_risk else None,
            "risk_reasons": list(self.last_risk.reasons) if self.last_risk else [],
        }

    @staticmethod
    def _account_digest(snapshot):
        return digest(snapshot.model_dump(mode="json", exclude={"at"})) if snapshot else None

    def _ai_reviews(self):
        # Staleness is judged when read: advice is stale once account content changes after it.
        current = self._account_digest(self.last_snapshot)
        reviews = self.store.records("supervisor", self.manifest.account, 1)
        for review in reviews:
            review["stale"] = (
                bool(review.get("stale")) or current is None or review.get("content_digest") != current
            )
        return reviews

    def _session_clock(self):
        now = self.now()
        day = self.manifest.active_session(now)
        if day is None:
            return {"session": None}
        ages = {"STK": [], "OPT": []}
        for i, q in self.last_quotes.items():
            if c := self.manifest.contract_map.get(i):
                ages[c.kind].append((now - q.observed_at).total_seconds())
        return {
            "session": str(day),
            "minutes_to_close": round((self.manifest.session_close(day) - now).total_seconds() / 60, 1),
            "accrual_days": self.manifest.accrual_days(day)
            if day in self.manifest.scoring_sessions
            else None,
            "options_tradable": self.manifest.can_trade(now, "OPT"),
            "stocks_tradable": self.manifest.can_trade(now, "STK"),
            # The oldest leg, not the freshest, decides whether trading is blocked.
            "oldest_stock_quote_age_s": round(max(ages["STK"]), 1) if ages["STK"] else None,
            "oldest_option_quote_age_s": round(max(ages["OPT"]), 1) if ages["OPT"] else None,
            "non_live_quotes": sum(q.data_type != "live" for q in self.last_quotes.values()),
        }

    async def _supervisor_loop(self):
        # Independent of quotes, optimization and operating mode. Failures cannot
        # stop broker processing, enable execution, or change the strategy.
        while True:
            if self.supervisor.due():
                await self._review()
            await asyncio.sleep(5)

    async def _review(self):
        try:
            sent = self.last_snapshot
            result = await self.supervisor.review(self.state())
            if result:
                current = self.last_snapshot
                # Snapshots are re-stamped every loop; only a change in account content makes advice stale.
                result["stale"] = (
                    not current
                    or not sent
                    or result["snapshot_id"] != sent.version
                    or current.model_dump(exclude={"at"}) != sent.model_dump(exclude={"at"})
                )
                result["content_digest"] = self._account_digest(sent)
                self.store.record("supervisor", self.manifest.account, result)
        except Exception as exc:
            self.alert("warning", f"AI supervisor unavailable; deterministic strategy continues: {exc}")

    async def control(self, action, profile=None, *, _automatic=False):
        if action == "profile" and (
            profile not in PROFILES or (not self.demo and profile not in self.supervisor.allowed_profiles)
        ):
            raise ValueError("Profile not validated for this contest")
        if not _automatic and action in ("pause", "shadow", "profile", "paper", "setup", "arm_setup"):
            self.autostart_pending = False  # an operator decision supersedes the launch-time start
            self.control_epoch += 1
            self.data_wait = None
            if action in ("pause", "shadow", "profile"):
                # Disarm immediately, including while an automatic preflight is
                # awaiting the broker. Its epoch check prevents late activation.
                self.execution.paused = True
                self.broker.readonly = True
        async with self.control_lock:
            activation_epoch = self.control_epoch
            if _automatic and (
                self.mode != "WAITING_DATA"
                or self.data_wait is None
                or self.data_wait["epoch"] != activation_epoch
            ):
                raise ValueError("Automatic activation superseded by an operator action")
            if action == "pause":
                self.setup_target_quantities = None
                self.setup_armed = self.auto_hedge_after_setup = False
                self.store.record("setup_schedule", self.manifest.account, {"status": "CANCELLED"})
                self._hard_stop("Operator requested pause")
                self.execution.recovery_checks = None
                await self.execution.pause()
            elif action == "shadow":
                self.pending_target = self.pending_recovery = None
                self.execution.recovery_checks = None
                self.setup_target_quantities = None
                self.setup_armed = self.auto_hedge_after_setup = False
                await self.execution.pause()
                if self.store.intents(self.manifest.account, True):
                    raise ValueError("Wait for pending cancellations/reconciliation before shadow mode")
                self.broker.readonly = True
                self.mode, self.force_optimize = "SHADOW", True
                self.pause_reason = None
                self.health = "Shadow monitoring enabled; no paper submissions"
            elif action in ("paper", "setup", "arm_setup"):
                if self.manifest.scoring_rule == "pending_interest_adjustment":
                    raise ValueError("Instructor's revised scoring formula required before activation")
                if not self.manifest.confirmed or not self.broker.qualified:
                    raise ValueError("Confirm contest configuration and broker contract identities first")
                if not self.demo and self.store.engine.dialect.name != "postgresql":
                    raise ValueError(
                        "Automatic IBKR paper trading requires PostgreSQL; SQLite is for demo/research"
                    )
                await self.reconcile()
                if (
                    self.execution.unresolved()
                    or self.store.intents(self.manifest.account, True)
                    or self.broker.snapshot().orders
                ):
                    raise ValueError("Resolve working or uncertain orders before enabling execution")
                if action == "arm_setup":
                    deadline = self.manifest.session_close(self.manifest.purchase_session) - timedelta(
                        minutes=self.manifest.execution.setup_cutoff_minutes
                    )
                    if self.now() >= deadline:
                        raise ValueError("Configured mandatory purchase window has already ended")
                    self.broker.readonly = True
                    self.execution.paused = True
                    self.setup_armed = self.auto_hedge_after_setup = True
                    self.mode = "WAITING_SETUP"
                    self.health = (
                        "Purchases armed for configured session open; quotes and limits revalidated then"
                    )
                    self.store.record(
                        "setup_schedule",
                        self.manifest.account,
                        {
                            "status": "ARMED",
                            "purchase_session": str(self.manifest.purchase_session),
                            "config_hash": self.manifest.config_hash,
                            "auto_hedge": True,
                        },
                    )
                    return self.state()
                if not self.last_scenarios or not self.last_quotes:
                    raise ValueError("Waiting for quotes/scenario/risk validation")
                # Reconciliation may have changed holdings or resolved a fill.
                # Activation always prices the CURRENT complete book, not an
                # older optimizer frame. Work stays off the broker event loop.
                self.last_snapshot, self.last_quotes = self.broker.snapshot(), self.broker.quotes()
                quote_coverage(self.last_quotes, self.last_snapshot.quantities())
                scenario_at = self.now()
                scenario_revision = self.broker_event_revision
                scenario_snapshot_id = self.last_snapshot.version
                ids = screen_snapshot(self.manifest, self.last_quotes, self.last_snapshot)
                self.last_scenarios = await asyncio.to_thread(
                    self.scenarios.build, self.last_snapshot, self.last_quotes, scenario_at, ids
                )
                self.last_scenario_at, self.last_scenario_revision = scenario_at, scenario_revision
                self.last_scenario_snapshot_id = scenario_snapshot_id
                if action == "paper":
                    # Score prerequisites precede risk optimization: otherwise a
                    # missing close makes optimize() return without a target and
                    # disguises that cause as an uncorrectable inventory breach.
                    try:
                        self.closing_target(self.broker.snapshot(), self.now())
                    except ValueError as exc:
                        self.last_target = None
                        self.health = f"Paper activation blocked: {exc}"
                        raise
                    result = self.gate.validate(
                        self.broker.snapshot(),
                        self.broker.quotes(),
                        self.last_scenarios,
                        self.broker.snapshot().quantities(),
                        self.now(),
                    )
                    if not result.passed and data_only(result.reasons):
                        raise DataUnavailable(
                            "Activation awaiting verified data: " + "; ".join(result.reasons)
                        )
                    if not result.passed:
                        # PAUSED has no proposal loop. Build a fresh, auditable
                        # target here instead of requiring a pre-existing target
                        # that paused operation can never produce.
                        self.last_target = None
                        self.force_optimize = True
                        self.last_snapshot = self.broker.snapshot()
                        self.last_quotes = self.broker.quotes()
                        await self.optimize()
                        correction = None
                        if self.last_target and self.last_target.status == "OPTIMAL":
                            correction = self.gate.validate(
                                self.broker.snapshot(),
                                self.broker.quotes(),
                                self.last_scenarios,
                                self.last_target.quantities,
                                self.now(),
                                2 * self.last_target.estimated_cost,
                            )
                        if correction is None or not correction.passed:
                            raise ValueError(
                                "Corrective hedge unavailable: "
                                + "; ".join(correction.reasons if correction else result.reasons)
                            )
                        # A fully compliant target is insufficient unless its
                        # first intermediate slice can actually reduce risk.
                        if not self.execution.feasible_slices(
                            self.last_target,
                            self.broker.snapshot(),
                            self.broker.quotes(),
                            self.last_scenarios,
                            self.now(),
                        ):
                            raise ValueError("Corrective hedge has no permitted risk-reducing first slice")
                else:
                    if (
                        not self.manifest.can_trade(self.now(), "OPT", setup=True)
                        or self.now().astimezone(NY).date() != self.manifest.purchase_session
                    ):
                        raise ValueError(
                            "Setup only permitted during the configured purchase-session option order window"
                        )
                    if self.now() < self.manifest.session_open(self.manifest.purchase_session) + timedelta(
                        minutes=self.manifest.execution.setup_opening_wait_minutes
                    ):
                        raise ValueError(
                            "Patient setup waits for the configured opening quote-stabilization period"
                        )
                    snapshot = self.broker.snapshot()
                    plan = await asyncio.to_thread(
                        self.setup_planner.plan,
                        snapshot,
                        dict(self.broker.quotes()),
                        self.last_scenarios,
                        self.now(),
                    )
                    if (
                        self.broker.snapshot().quantities() != snapshot.quantities()
                        or self.broker.snapshot().orders
                    ):
                        raise ValueError("Inventory/orders changed during setup planning; reconcile")
                    self.setup_pairs = plan["pairs"]
                    self.setup_target = plan["target"]
                    proposed = dict(self.setup_target.quantities)
                    preflight = self.gate.validate(
                        snapshot,
                        self.broker.quotes(),
                        self.last_scenarios,
                        proposed,
                        self.now(),
                        day_zero=True,
                    )
                    if not preflight.passed:
                        raise ValueError("Mandatory portfolio preflight: " + "; ".join(preflight.reasons))
                    self.setup_target_quantities = dict(proposed)
                    self.setup_decision_id = self.store.record(
                        "decision",
                        snapshot.account,
                        {
                            **self.setup_target.model_dump(mode="json"),
                            "reference_midpoints": {str(i): q.mid for i, q in self.last_quotes.items()},
                            "profile": "PROTECTED_DAY0",
                            "pairs": self.setup_pairs,
                            "at": self.now().isoformat(),
                            "risk_passed": True,
                        },
                    )
                    self.store.record(
                        "setup_plan",
                        snapshot.account,
                        {
                            "quantities": proposed,
                            "pairs": self.setup_pairs,
                            "acquisition_path": plan["acquisition_path"],
                            "scenario_id": self.last_scenarios.identifier,
                            "snapshot_id": snapshot.version,
                            "config_hash": self.manifest.config_hash,
                            "explanation": "Complete mandatory + protective option + residual stock plan; guaranteed paired acquisitions only",
                        },
                    )
                if activation_epoch != self.control_epoch:
                    raise ValueError(
                        "Activation superseded by an operator action; execution remains disabled"
                    )
                if _automatic:
                    self.store.record(
                        "continuity",
                        self.manifest.account,
                        {
                            "status": "RESUMED",
                            "mode": "PAPER" if action == "paper" else "SETUP",
                            "config_hash": self.manifest.config_hash,
                            "at": self.now().isoformat(),
                            "snapshot_id": self.broker.snapshot().version,
                        },
                    )
                self.data_wait = None
                self.pause_reason = None
                self.ready = action == "paper" and result.passed
                if action == "paper":
                    self.last_risk = result
                self.broker.readonly = False
                self.execution.paused = self.execution.pause_requested = False
                self.setup_armed = False
                self.mode, self.force_optimize = ("PAPER" if action == "paper" else "SETUP"), True
                if action == "paper" and not result.passed:
                    self.pending_target = self.last_target
                    self.active_plan_started = False
                    self.health = "Paper enabled: executing independently validated corrective hedge"
                else:
                    self.health = (
                        "Paper trading enabled; fresh activation checks passed"
                        if action == "paper"
                        else "Protected setup enabled; fresh preflight passed"
                    )
            elif action == "reconcile":
                await self.reconcile()
                if not self.demo and self.market_data_errors:
                    self.broker.refresh_market_data()
            elif action == "profile":
                self.setup_armed = self.auto_hedge_after_setup = False
                if profile not in PROFILES:
                    raise ValueError("Unknown profile")
                if not self.demo and profile not in self.supervisor.allowed_profiles:
                    raise ValueError("Profile not validated for this contest")
                await self.execution.pause()
                self.mode = "SHADOW"
                self.pause_reason = None
                self.health = "Profile changed; shadow monitoring enabled"
                self.pending_target = None
                self.active_plan_started = False
                self.broker.readonly = True
                self.profile, self.force_optimize = profile, True
                self.store.record(
                    "profile",
                    self.manifest.account,
                    {"profile": profile, "config_hash": self.manifest.config_hash},
                )
            else:
                raise ValueError("Unknown control")
            return self.state()

    def surplus_snapshot(self):
        """Read-only research export. Never contacts the broker or creates an intent."""
        snap, scenarios = self.last_snapshot, self.last_scenarios
        if snap is None:
            raise ValueError("Waiting for the main app's reconciled portfolio snapshot")
        now = self.now()
        day = self.manifest.active_session(now)
        previous = self.ledger.previous_close(day) if day in self.manifest.scoring_sessions else None
        target = self.ledger.target_for(day, previous, allow_estimate=True) if previous is not None else None
        official_target = self.ledger.target_for(day, previous) if previous is not None else None
        try:
            estimate = self.nav_tracker.estimate(snap, self.last_quotes, self.manifest.contract_map)
        except ValueError:
            estimate = None
        return payload(
            {
                "protocol": "surplus-research-v1",
                "generated_at": now,
                "manifest": self.manifest.model_dump(mode="json"),
                "snapshot": snap.model_dump(mode="json"),
                "quotes": {i: q.model_dump(mode="json") for i, q in self.last_quotes.items()},
                "scenarios": None
                if scenarios is None
                else {
                    "ids": scenarios.ids,
                    "changes": scenarios.changes.tolist(),
                    "weights": scenarios.weights.tolist(),
                    "stress": scenarios.stress.tolist(),
                    "calendar_days": scenarios.calendar_days,
                    "source": scenarios.source,
                    "identifier": scenarios.identifier,
                },
                "scenario_at": self.last_scenario_at,
                "scenario_snapshot_id": self.last_scenario_snapshot_id,
                "scenario_revision": self.last_scenario_revision,
                "broker_revision": self.broker_event_revision,
                "baseline_nav": self.ledger.closes().get(self.manifest.day_zero),
                "nav_estimate": estimate,
                "target": target,
                "target_estimated": target is not None and official_target is None,
                "forecast_close": self.manifest.next_close(self.last_scenario_at or now),
                "main_mode": self.mode,
                "execution_busy": bool(
                    snap.orders
                    or self.pending_target
                    or self.pending_recovery
                    or self.store.intents(self.manifest.account, True)
                    or self.execution.burn_open()
                ),
                "config_hash": self.manifest.config_hash,
                "snapshot_id": snap.version,
            }
        )

    def state(self):
        snap = self.last_snapshot
        day = self.manifest.active_session(self.now())
        previous_record = self.ledger.previous_close_record(day) if day else None
        previous = previous_record["nav"] if previous_record else None
        self._warn_api_previous_close(day, previous_record)
        closing_target = self.ledger.target_for(day, previous, allow_estimate=True) if day else None
        try:
            estimate = (
                self.nav_tracker.estimate(snap, self.last_quotes, self.manifest.contract_map)
                if snap
                else None
            )
        except ValueError:
            estimate = None
        positions = []
        if snap:
            for p in snap.positions:
                c = self.manifest.contract_map.get(p.con_id)
                q = self.last_quotes.get(p.con_id)
                positions.append(
                    {
                        "con_id": p.con_id,
                        "label": c.label if c else f"Unknown conId {p.con_id}",
                        "quantity": p.quantity,
                        "role": "MANDATORY" if p.con_id in self.manifest.mandatory else "HEDGE",
                        "mark": q.mid if q else None,
                        "value": p.quantity * c.multiplier * q.mid if c and q else None,
                    }
                )
        return payload(
            {
                "contest": self.manifest.name,
                "account": self.manifest.account,
                "demo": self.demo,
                "risk_factors": {
                    "beta_benchmark": self.manifest.beta_benchmark,
                    "beta_source": self.last_scenarios.beta_source if self.last_scenarios else "Unavailable",
                    "rho_source": "American-option model; dollars per 1 percentage-point rate change",
                    "spot_vol_correlation": getattr(self.last_scenarios, "spot_vol_correlation", {}),
                },
                "rehearsal": self.rehearsal,
                "day_zero": str(self.manifest.day_zero),
                "purchase_session": str(self.manifest.purchase_session),
                "setup": {
                    "armed": self.setup_armed,
                    "auto_hedge": self.auto_hedge_after_setup,
                    "plan": self.setup_target_quantities,
                    "pairs": self.setup_pairs,
                    "opening_wait_minutes": self.manifest.execution.setup_opening_wait_minutes,
                    "complete": bool(snap) and not self.gate.registry.check(snap.quantities()),
                    "holdings": [
                        {
                            "con_id": i,
                            "label": self.manifest.contract_map[i].label,
                            "required": q,
                            "held": snap.quantities().get(i, 0) if snap else 0,
                            "working": (snap.projected().get(i, 0) - snap.quantities().get(i, 0))
                            if snap
                            else 0,
                        }
                        for i, q in self.manifest.mandatory.items()
                    ],
                    "baseline_recorded": self.manifest.day_zero in self.ledger.closes(),
                    "on_day_zero": self.now().astimezone(NY).date() == self.manifest.day_zero,
                    "window_open": self.now().astimezone(NY).date() == self.manifest.purchase_session
                    and self.manifest.can_trade(self.now(), "OPT", setup=True),
                },
                "mode": self.mode,
                "ready": self.ready,
                "health": self.health,
                "clock": self.now().isoformat(),
                "snapshot_id": snap.version if snap else None,
                "connected": self.broker.connected if self.demo else self.broker.ib.isConnected(),
                "broker_qualified": self.broker.qualified,
                "unavailable_instruments": self.broker.qualification_errors if not self.demo else {},
                "nav": str(snap.nav) if snap else None,
                "cash": str(snap.cash) if snap else None,
                "accrued_cash": str(snap.accrued_cash) if snap and snap.accrued_cash is not None else None,
                "interest": {
                    "annual_rate": self.manifest.policy.cash_rate,
                    "rate_status": self.manifest.policy.cash_rate_status,
                    "rate_source": self.manifest.policy.cash_rate_source,
                    "day_count": self.manifest.policy.cash_day_count,
                    "free_balance": self.manifest.policy.cash_interest_free_balance,
                    "note": "Accrued cash is already in NAV. Future interest uses a disclosed cash/settlement proxy; official NAV controls scoring.",
                },
                "nav_estimate": estimate,
                "previous_close": str(previous) if previous is not None else None,
                # "statement" (official IBKR statement) or "api" (provisional recorder capture).
                "previous_close_provenance": previous_record["provenance"] if previous_record else None,
                "closing_target": str(closing_target) if closing_target is not None else None,
                "closing_target_estimated": self.manifest.scoring_rule == "actual_daily_cash_rate"
                and day not in self.ledger.daily_rates(),
                "account_nav_at": snap.account_at.isoformat() if snap else None,
                "nav_report_source": self.manifest.report_source,
                "official_account_nav": self.ledger.latest_report(),
                "closing_recorder": self.closing_recorder.state(),
                "provisional_score": abs(estimate - float(closing_target))
                if estimate is not None and closing_target is not None
                else None,
                "scores": self._scores_with_provisional_warning(),
                "baseline_reference": self.store.records("baseline_reference", self.manifest.account, 1),
                "positions": positions,
                # Bounded views keep the 2 s dashboard poll cheap as burner legs accumulate;
                # CSV exports read the complete history.
                "orders": self.store.intents(self.manifest.account, limit=STATE_ROWS),
                "fills": self.store.fills(self.manifest.account, limit=STATE_ROWS),
                "execution_quality": execution_quality(
                    self.store, self.manifest.account, self.manifest.contract_map, limit=STATE_ROWS
                ),
                "package_executions": self.store.records("package_execution", self.manifest.account, 100),
                "package_commissions": self.store.records("package_commission", self.manifest.account, 100),
                "transitions": self.store.records("order_transition", self.manifest.account, 100),
                "decisions": self.store.records("decision", self.manifest.account, 30),
                "alerts": self.store.records("alert", self.manifest.account, 30),
                "risk": self.last_risk.model_dump(mode="json") if self.last_risk else None,
                "ai": self._ai_reviews(),
                "ai_enabled": self.supervisor.enabled,
                "ai_status": self.supervisor.status(),
                "recovery": {**self.recovery.latest, "promotion": self.recovery.promotion_status()},
                "burner": self.burner.state(),
                "autostart": {
                    "mode": self.autostart_mode(),
                    "configured": self.manifest.autostart,
                    "source": "configuration" if self.autostart_override is None else "dashboard",
                    "pending": self.autostart_pending,
                    "active": bool(self.data_wait and self.data_wait.get("autostart")),
                },
                "profile": self.profile,
                "profiles": list(PROFILES if self.demo else self.supervisor.allowed_profiles),
                "pause_pending": self.execution.pause_requested and not self.execution.pause_complete(),
                "continuity": {
                    "automatic_resume_pending": self.mode == "WAITING_DATA" and self.data_wait is not None,
                    "wait": self.data_wait,
                    "pause_reason": self.pause_reason,
                    "retry_seconds": 15,
                },
                "contract_labels": {i: c.label for i, c in self.manifest.contract_map.items()},
                "quote_count": len(self.last_quotes),
                # Label what execution actually uses, not merely whether a Moomoo file exists.
                "market_data_provider": "synthetic"
                if self.demo
                else "moomoo (data only)"
                if self.manifest.market_data_provider == "moomoo"
                else "IBKR (Moomoo display only, not used for execution)"
                if self.moomoo_data and self.moomoo_data.path.exists()
                else "IBKR",
                "market_data_execution_verified": bool(self.last_quotes)
                and all(q.data_type == "live" for q in self.last_quotes.values())
                and set(self.manifest.mandatory).issubset(self.last_quotes)
                if self.manifest.market_data_provider == "moomoo" and not self.demo
                else None,
                "unquoted_tradable_contracts": [
                    self.manifest.contract_map[i].label
                    for i in self.manifest.allowlist
                    if i not in self.last_quotes
                ][:20],
                "market_data_received_at": self.moomoo_data.received_at if self.moomoo_data else None,
                "market_data_notes": self.moomoo_data.errors if self.moomoo_data else [],
                "market_quotes": [
                    {
                        "label": self.manifest.contract_map[i].label,
                        "bid": q.bid,
                        "ask": q.ask,
                        "at": q.observed_at.isoformat(),
                        "data_type": q.data_type,
                    }
                    for i, q in self.last_quotes.items()
                ],
                "market_data_errors": list(self.market_data_errors.values()),
                "config_hash": self.manifest.config_hash,
                "contest_id": self.manifest.contest_id,
                "policy": self.manifest.policy.model_dump(),
                "execution_policy": self.manifest.execution.model_dump(),
                "limits": self.manifest.risk.model_dump(),
                "last_quote_at": max(
                    (q.observed_at.isoformat() for q in self.last_quotes.values()), default=None
                ),
                "scoring_sessions": [str(d) for d in self.manifest.scoring_sessions],
                "session_clock": self._session_clock(),
                "open_intent_count": len(self.store.intents(self.manifest.account, True)),
                "confirmed": self.manifest.confirmed,
            }
        )
