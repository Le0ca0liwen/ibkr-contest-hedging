from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from .continuity import quote_coverage
from .models import OrderRequest, SetupPackageRequest, Target
from .optimizer import execution_cost
from .order_policy import choose_order, rounded_price, spread_error, urgency_for
from .scenarios import ScenarioEngine
from .storage import TERMINAL


class ExecutionManager:
    def __init__(self, manifest, store, broker, gate):
        self.manifest, self.store, self.broker, self.gate = manifest, store, broker, gate
        self.lock = asyncio.Lock()
        self.paused = True
        self.pause_requested = False
        self.sent_at = {}
        self.repriced = set()
        self.last_preview = None
        self.preview_valid = False
        self.owned_inventory_ranges = {}
        self.recovery_checks = None
        self.score_context = None
        self.current_scenarios = None
        self.scenario_builder = ScenarioEngine(manifest).build
        self.clock = None

    def execution_time(self, fallback):
        if self.clock is not None:
            return self.clock()
        if self.gate.demo:
            return fallback
        from .models import utcnow

        return utcnow()

    async def refresh_scenarios(self, scenarios, now):
        """Build off-loop, then verify inputs again; never transmit a moving frame."""
        snapshot, quotes = self.broker.snapshot(), self.broker.quotes()
        clock = self.execution_time(now)
        max_age = self.manifest.policy.risk_seconds
        if scenarios is None:
            raise ValueError("Missing execution scenarios; replan required")
        if not scenarios.is_current(quotes, clock, max_age):
            # Recovery changes the forecast distribution. Rebuilding ordinary
            # scenarios here would silently discard that strategy's forecast.
            if self.recovery_checks or "; recovery " in scenarios.source:
                raise ValueError("Recovery scenario inputs changed; replan required")
            quote_coverage(quotes, scenarios.ids)
            scenarios = await asyncio.to_thread(
                self.scenario_builder, snapshot, dict(quotes), clock, scenarios.ids
            )
            snapshot, quotes = self.broker.snapshot(), self.broker.quotes()
            clock = self.execution_time(now)
            if not scenarios.is_current(quotes, clock, max_age):
                raise ValueError("Market changed during scenario refresh; replan required")
        self.current_scenarios = scenarios
        return snapshot, quotes, clock, scenarios

    async def cancel_order(self, order_id):
        identifiers = [
            r["id"]
            for r in self.store.intents(self.manifest.account, True)
            if r["broker_order_id"] == order_id
        ]
        self.store.record(
            "cancel_request", self.manifest.account, {"order_id": order_id, "intent_ids": identifiers}
        )
        await self.broker.cancel(order_id)

    def economic_check(self, snapshot, quotes, scenarios, quantities, now):
        if self.score_context is None:
            return True
        quote_coverage(quotes, scenarios.ids)
        from .optimizer import HedgeOptimizer

        previous, estimate = self.score_context(snapshot, quotes, now)
        target = Target(
            quantities=quantities,
            expected_score=0,
            current_score=0,
            uncertainty=0,
            status="SLICE",
            snapshot_id=snapshot.version,
            scenario_id=scenarios.identifier,
            config_hash=self.manifest.config_hash,
        )
        evaluated = HedgeOptimizer(self.manifest).evaluate_target(
            snapshot, quotes, scenarios, target, previous, estimate
        )
        exposure_only = HedgeOptimizer(self.manifest).evaluate_target(
            snapshot, quotes, scenarios, target, previous, estimate, include_costs=False
        )
        return (
            exposure_only.current_score - exposure_only.expected_score > 0.01
            and evaluated.current_score - evaluated.expected_score
            > max(
                self.manifest.execution.minimum_slice_improvement,
                self.manifest.policy.uncertainty_multiplier * evaluated.uncertainty,
            )
        )

    def unresolved(self):
        return [
            r
            for r in self.store.intents(self.manifest.account, True)
            if r["state"] in ("SUBMITTING", "UNKNOWN")
        ]

    def feasible_slices(self, target, snapshot, quotes, scenarios, now, setup=False):
        """Read-only execution-path check, shared by activation and submission."""
        actual = snapshot.quantities()
        candidates = []
        current_risk = None
        for i in sorted(set(target.quantities) | set(actual)):
            difference = target.quantities.get(i, 0) - actual.get(i, 0)
            if not difference:
                continue
            c, q = self.manifest.contract_map.get(i), quotes.get(i)
            if c is None or q is None:
                continue
            size = min(
                abs(difference),
                self.manifest.execution.option_slice
                if c.kind == "OPT"
                else self.manifest.execution.stock_slice,
                q.ask_size if difference > 0 else q.bid_size,
            )
            if size <= 0:
                continue
            signed = size if difference > 0 else -size
            partial = dict(actual)
            partial[i] = partial.get(i, 0) + signed
            costs = execution_cost(self.manifest, quotes, [i], [signed])
            if current_risk is None:
                current_risk = self.gate.engine.evaluate(snapshot, quotes, scenarios, actual)
            if setup and i in self.manifest.mandatory:
                continue  # Mandatory acquisitions can only use protected packages.
            if spread_error(self.manifest, c, q):
                continue
            if (
                c.kind == "STK"
                and current_risk.passed
                and not setup
                and abs(signed) * q.mid < self.manifest.execution.minimum_stock_notional
            ):
                continue
            if (
                current_risk.passed
                and not setup
                and not self.economic_check(snapshot, quotes, scenarios, partial, now)
            ):
                continue
            result = self.gate.validate(
                snapshot,
                quotes,
                scenarios,
                partial,
                now,
                costs=2 * costs,
                corrective=not current_risk.passed,
                day_zero=setup,
            )
            extra = (
                self.recovery_checks(snapshot, quotes, scenarios, partial, 2 * costs)
                if self.recovery_checks
                else []
            )
            if result.passed and not extra:
                path_safe = True
                for amount in range(1, abs(signed)):
                    intermediate = {**actual, i: actual.get(i, 0) + amount * (1 if signed > 0 else -1)}
                    step_cost = execution_cost(
                        self.manifest, quotes, [i], [amount * (1 if signed > 0 else -1)]
                    )
                    if not self.gate.validate(
                        snapshot,
                        quotes,
                        scenarios,
                        intermediate,
                        now,
                        2 * step_cost,
                        corrective=not current_risk.passed,
                        day_zero=setup,
                    ).passed:
                        path_safe = False
                        break
                    if self.recovery_checks and self.recovery_checks(
                        snapshot, quotes, scenarios, intermediate, 2 * step_cost
                    ):
                        path_safe = False
                        break
                if path_safe:
                    candidates.append((result.metrics.get("stress_loss", float("inf")), i, signed, result))
        return candidates

    async def submit_slice(self, target, decision_id, snapshot, quotes, scenarios, now, setup=False):
        self.current_scenarios = scenarios
        async with self.lock:
            if self.paused or self.pause_requested or self.unresolved():
                return {"submitted": False, "reason": "Paused or unresolved submission"}
            if (
                hasattr(self.broker, "margin_preview")
                and self.last_preview is not None
                and not self.preview_valid
                and now - self.last_preview < timedelta(minutes=1)
            ):
                return {
                    "submitted": False,
                    "reason": "Prior credit preview failed; awaiting rate-limited retry",
                }
            if self.store.intents(snapshot.account, True) or snapshot.orders:
                return {"submitted": False, "reason": "Working orders must reconcile before a new slice"}
            candidates = self.feasible_slices(target, snapshot, quotes, scenarios, now, setup)
            actual = snapshot.quantities()
            if not candidates:
                return {
                    "submitted": False,
                    "reason": "No economical execution slice meets score, uncertainty, liquidity and risk checks",
                }
            _, identifier, signed, result = min(candidates, key=lambda x: (x[0], x[1]))
            partial = dict(actual)
            partial[identifier] = partial.get(identifier, 0) + signed
            costs = execution_cost(self.manifest, quotes, [identifier], [signed])
            q = quotes[identifier]
            corrective = not self.gate.engine.evaluate(snapshot, quotes, scenarios, actual).passed
            try:
                policy = choose_order(
                    self.manifest,
                    self.broker,
                    self.manifest.contract_map[identifier],
                    q,
                    signed,
                    now,
                    corrective,
                    setup,
                )
            except ValueError as exc:
                return {"submitted": False, "reason": str(exc)}
            price = policy["price"]
            costs = max(costs, policy["slippage_reserve"] + self.manifest.policy.minimum_commission)
            if not self.gate.validate(
                snapshot, quotes, scenarios, partial, now, 2 * costs, corrective=corrective, day_zero=setup
            ).passed:
                return {"submitted": False, "reason": "Adverse execution reserve violates risk limits"}
            request = OrderRequest(
                intent_id=uuid4().hex,
                con_id=identifier,
                signed_quantity=signed,
                limit_price=price,
                account=snapshot.account,
                snapshot_id=snapshot.version,
                decision_id=decision_id,
                order_type=policy["order_type"],
                urgency=policy["urgency"],
            )
            # Durable before reserving/submitting. Any later crash leaves a
            # recoverable intent and NEVER grants permission to resend it.
            self.store.create_intent(request)
            self.store.record(
                "execution_plan",
                snapshot.account,
                {
                    "intent_id": request.intent_id,
                    **policy,
                    "initial_limit": str(price),
                    "setup": setup,
                    "quantities": partial,
                    "scenarios": scenarios.identifier,
                    "quote_at": q.observed_at.isoformat(),
                },
            )
            self.store.record(
                "risk", snapshot.account, {"intent_id": request.intent_id, **result.model_dump(mode="json")}
            )
            self.store.transition(request.intent_id, "VALIDATED")
            # Credit previews supplement (never replace) deterministic risk. Rate
            # limit to one/minute; each subsequent slice still has local checks.
            if hasattr(self.broker, "margin_preview") and (
                self.last_preview is None or now - self.last_preview >= timedelta(minutes=1)
            ):
                try:
                    self.last_preview = now
                    self.preview_valid = False
                    preview = await asyncio.wait_for(self.broker.margin_preview(request), 15)
                    change = Decimal(str(preview.initMarginChange))
                    if (
                        not change.is_finite()
                        or float(snapshot.excess_liquidity - max(Decimal(0), change))
                        < self.manifest.risk.liquidity_buffer * result.metrics["budget"]
                    ):
                        raise ValueError("Broker credit preview violates liquidity buffer")
                    if preview.warningText:
                        raise ValueError(f"Broker credit preview warning: {preview.warningText}")
                    self.preview_valid = True
                except Exception as exc:
                    self.store.transition(request.intent_id, "REJECTED")
                    return {"submitted": False, "reason": f"Credit/risk preflight blocked: {exc}"}
            try:
                fresh, qs, clock, scenarios = await self.refresh_scenarios(scenarios, now)
                if self.paused or self.pause_requested or self.broker.readonly:
                    raise ValueError("Execution was paused during preflight")
                if fresh.quantities() != actual or fresh.orders:
                    raise ValueError("Inventory/orders changed before transmission")
                current = self.gate.engine.evaluate(fresh, qs, scenarios, actual)
                chosen = choose_order(
                    self.manifest,
                    self.broker,
                    self.manifest.contract_map[identifier],
                    qs[identifier],
                    signed,
                    clock,
                    not current.passed,
                    setup,
                )
                if (float(chosen["price"]) - policy["price_cap"]) * (1 if signed > 0 else -1) > 1e-9:
                    raise ValueError("Fresh price exceeds original modeled budget")
                costs = max(
                    costs,
                    execution_cost(self.manifest, qs, [identifier], [signed]),
                    chosen["slippage_reserve"]
                    + max(
                        self.manifest.policy.minimum_commission,
                        abs(signed)
                        * (
                            self.manifest.policy.commission_option
                            if self.manifest.contract_map[identifier].kind == "OPT"
                            else self.manifest.policy.commission_share
                        ),
                    ),
                )
                gate = self.gate.validate(
                    fresh,
                    qs,
                    scenarios,
                    partial,
                    clock,
                    2 * costs,
                    corrective=not current.passed,
                    day_zero=setup,
                )
                if not gate.passed:
                    raise ValueError("; ".join(gate.reasons))
                if self.recovery_checks:
                    extra = self.recovery_checks(fresh, qs, scenarios, partial, 2 * costs)
                    if extra:
                        raise ValueError("; ".join(extra))
                if (
                    current.passed
                    and not setup
                    and not self.economic_check(fresh, qs, scenarios, partial, clock)
                ):
                    raise ValueError("Slice no longer improves expected score after costs/uncertainty")
                fresh_candidates = self.feasible_slices(
                    target.model_copy(update={"quantities": partial}), fresh, qs, scenarios, clock, setup
                )
                if not any(i == identifier and size == signed for _, i, size, _ in fresh_candidates):
                    raise ValueError("Fresh scenario/liquidity checks require a new execution slice")
                request = request.model_copy(
                    update={
                        "order_type": chosen["order_type"],
                        "urgency": chosen["urgency"],
                        "limit_price": chosen["price"],
                        "snapshot_id": fresh.version,
                    }
                )
                self.store.prepare_request(
                    request,
                    {**chosen, "scenario_id": scenarios.identifier, "scenario_at": scenarios.built_at},
                )
            except Exception as exc:
                self.store.transition(request.intent_id, "REJECTED")
                return {"submitted": False, "reason": f"Fresh execution validation: {exc}"}
            order_id = self.broker.reserve_order_id()
            self.store.transition(request.intent_id, "SUBMITTING", order_id=order_id)
            self.owned_inventory_ranges[identifier] = (
                min(actual.get(identifier, 0), partial[identifier]),
                max(actual.get(identifier, 0), partial[identifier]),
            )
            try:
                evidence = await (
                    self.broker.setup_mandate(request, order_id, now)
                    if setup and request.con_id in self.manifest.mandatory
                    else self.broker.submit(request, order_id)
                )
                # Local placeOrder() return is NOT broker acknowledgement.
                self.sent_at[request.intent_id] = clock
                self.store.record(
                    "submission", snapshot.account, {"intent_id": request.intent_id, **evidence}
                )
                return {"submitted": True, "intent_id": request.intent_id, "order_id": order_id}
            except Exception as exc:
                self.store.transition(request.intent_id, "UNKNOWN")
                self.store.record(
                    "alert",
                    snapshot.account,
                    {
                        "severity": "error",
                        "message": f"Uncertain order submission: {exc}",
                        "intent_id": request.intent_id,
                    },
                )
                self.paused = True
                return {"submitted": False, "reason": "Submission uncertain; reconcile before any new trade"}

    async def submit_package(self, buy_id, sell_id, decision_id, snapshot, quotes, scenarios, now):
        if self.manifest.contest_rules == "spcx_rklb_spy":
            return {"submitted": False, "reason": "Automatic seven-leg contest setup is unsupported"}
        self.current_scenarios = scenarios
        async with self.lock:
            if (
                self.paused
                or self.pause_requested
                or self.store.intents(snapshot.account, True)
                or snapshot.orders
            ):
                return {"submitted": False, "reason": "Protected setup waits for reconciled orders"}
            actual = snapshot.quantities()
            if actual.get(buy_id, 0) >= self.manifest.mandatory[buy_id]:
                return {"submitted": False, "reason": "Mandatory leg already complete"}
            for i in (buy_id, sell_id):
                error = spread_error(self.manifest, self.manifest.contract_map[i], quotes[i])
                if error:
                    return {"submitted": False, "reason": error}
            a, b = quotes[buy_id], quotes[sell_id]
            if min(a.ask_size, b.bid_size) < 1:
                return {"submitted": False, "reason": "No paired option liquidity"}
            mid = a.mid - b.mid
            if mid <= 0:
                return {"submitted": False, "reason": "Protected debit package has inconsistent leg prices"}
            price = rounded_price(mid, 0.01, 1)
            close = self.manifest.session_close(self.manifest.purchase_session)
            if (close - now).total_seconds() <= 300:
                price = rounded_price(a.ask - b.bid, 0.01, 1)
            partial = {**actual, buy_id: actual.get(buy_id, 0) + 1, sell_id: actual.get(sell_id, 0) - 1}
            costs = execution_cost(self.manifest, quotes, [buy_id, sell_id], [1, -1])
            risk = self.gate.validate(snapshot, quotes, scenarios, partial, now, 2 * costs, day_zero=True)
            if not risk.passed:
                return {"submitted": False, "reason": "Protected package risk: " + "; ".join(risk.reasons)}
            request = SetupPackageRequest(
                package_id=uuid4().hex,
                buy_id=buy_id,
                sell_id=sell_id,
                quantity=1,
                limit_price=price,
                account=snapshot.account,
                snapshot_id=snapshot.version,
                decision_id=decision_id,
            )
            leg_ids = [f"{request.package_id}:{i}" for i in (buy_id, sell_id)]
            self.store.create_package(request, quotes)
            for identifier in leg_ids:
                self.store.transition(identifier, "VALIDATED")
            try:
                if hasattr(self.broker, "package_preview"):
                    preview = await asyncio.wait_for(self.broker.package_preview(request), 15)
                    change = Decimal(str(preview.initMarginChange))
                    if (
                        not change.is_finite()
                        or preview.warningText
                        or snapshot.excess_liquidity - max(Decimal(0), change)
                        < Decimal(str(self.manifest.risk.liquidity_buffer * risk.metrics["budget"]))
                    ):
                        raise ValueError("Guaranteed package credit preview rejected")
                fresh, qs, clock, scenarios = await self.refresh_scenarios(scenarios, now)
                if self.paused or self.pause_requested or self.broker.readonly:
                    raise ValueError("Protected setup was paused during preflight")
                if fresh.quantities() != actual or fresh.orders:
                    raise ValueError("Inventory/orders changed during package preview")
                for i in (buy_id, sell_id):
                    if spread_error(self.manifest, self.manifest.contract_map[i], qs[i]):
                        raise ValueError("Package spread widened during preview")
                cap = mid + (a.ask - a.bid + b.ask - b.bid) * (
                    0.5 + self.manifest.policy.slippage_spread_fraction
                )
                costs = max(costs, execution_cost(self.manifest, qs, [buy_id, sell_id], [1, -1]))
                if (
                    float(price) > cap
                    or not self.gate.validate(
                        fresh, qs, scenarios, partial, clock, 2 * costs, day_zero=True
                    ).passed
                ):
                    raise ValueError("Fresh package risk/freshness validation failed")
                self.store.record(
                    "package_pretransmission",
                    snapshot.account,
                    {
                        "package_id": request.package_id,
                        "scenario_id": scenarios.identifier,
                        "scenario_at": scenarios.built_at,
                        "snapshot_id": fresh.version,
                    },
                )
            except Exception as exc:
                for identifier in leg_ids:
                    self.store.transition(identifier, "REJECTED")
                return {"submitted": False, "reason": f"Protected setup preflight: {exc}"}
            oid = self.broker.reserve_order_id()
            for identifier in leg_ids:
                self.store.transition(identifier, "SUBMITTING", oid)
            for i in (buy_id, sell_id):
                self.owned_inventory_ranges[i] = (
                    min(actual.get(i, 0), partial[i]),
                    max(actual.get(i, 0), partial[i]),
                )
            try:
                evidence = await self.broker.submit_package(request, oid, clock)
                for identifier in leg_ids:
                    self.sent_at[identifier] = clock
                self.store.record(
                    "package_submission", snapshot.account, {"package_id": request.package_id, **evidence}
                )
                return {"submitted": True, "package_id": request.package_id, "order_id": oid}
            except Exception as exc:
                for identifier in leg_ids:
                    self.store.transition(identifier, "UNKNOWN")
                self.paused = True
                return {
                    "submitted": False,
                    "reason": f"Protected package submission uncertain; reconcile, never resend: {exc}",
                }

    def on_order(self, event):
        reference = event.get("order_ref", "")
        if reference.startswith("hedge-package:"):
            package_id = reference.removeprefix("hedge-package:")
            package = self.store.get_record(package_id, self.manifest.account, "setup_package")
            if package is None:
                self.paused = True
                raise ValueError("Broker protected package has no durable intent")
            if event.get("order_type") == "LMT" and event.get("limit_price", 0) > 0:
                self.store.update_package_price(package_id, self.manifest.account, event["limit_price"])
            for i in (package["buy_id"], package["sell_id"]):
                identifier = f"{package_id}:{i}"
                rows = {r["id"]: r for r in self.store.intents(self.manifest.account)}
                row = rows[identifier]
                status = event.get("status")
                state = {
                    "Submitted": "ACKNOWLEDGED",
                    "PreSubmitted": "ACKNOWLEDGED",
                    "Filled": "ACKNOWLEDGED",
                    "Cancelled": "CANCELLED",
                    "ApiCancelled": "CANCELLED",
                    "Inactive": "REJECTED",
                    "PendingCancel": "CANCEL_PENDING",
                }.get(status)
                if state is None or row["state"] in TERMINAL:
                    continue
                if state == "ACKNOWLEDGED" and row["state"] in ("PARTIALLY_FILLED", "CANCEL_PENDING"):
                    continue
                if state == "CANCELLED" and row["state"] == "SUBMITTING":
                    state = "UNKNOWN"
                self.store.transition(identifier, state, event.get("order_id"), event.get("perm_id"))
            return
        if not reference.startswith("hedge:"):
            return
        identifier = reference.removeprefix("hedge:")
        rows = {r["id"]: r for r in self.store.intents(self.manifest.account)}
        if identifier not in rows:
            self.paused = True
            self.store.record(
                "alert",
                self.manifest.account,
                {"severity": "error", "message": "Broker system order has no durable intent"},
            )
            return
        row = rows[identifier]
        if event.get("order_type") == "LMT" and event.get("limit_price", 0) > 0:
            self.store.observe_limit(identifier, event["limit_price"])
        status = event.get("status")
        new = {
            "Submitted": "PARTIALLY_FILLED" if event.get("filled", 0) else "ACKNOWLEDGED",
            "PreSubmitted": "ACKNOWLEDGED",
            "Filled": "FILLED",
            "Cancelled": "CANCELLED",
            "ApiCancelled": "CANCELLED",
            "Inactive": "REJECTED",
            "PendingCancel": "CANCEL_PENDING",
        }.get(status)
        if new is None:
            return
        if row["state"] in TERMINAL and new != "FILLED":
            return  # stale callback cannot reopen a terminal order
        if row["state"] in ("PARTIALLY_FILLED", "CANCEL_PENDING") and new == "ACKNOWLEDGED":
            return
        if row["state"] == "SUBMITTING" and new == "CANCELLED":
            self.store.transition(identifier, "UNKNOWN")
        try:
            self.store.transition(identifier, new, event.get("order_id"), event.get("perm_id"))
        except ValueError:
            self.paused = True
            self.store.record(
                "alert",
                self.manifest.account,
                {
                    "severity": "error",
                    "message": "Unexpected broker order transition; reconcile",
                    "event": event,
                },
            )

    def on_execution(self, execution):
        if execution.order_ref.startswith("hedge-package:"):
            package_id = execution.order_ref.removeprefix("hedge-package:")
            package = self.store.get_record(package_id, self.manifest.account, "setup_package")
            if package is None or execution.con_id not in (package["buy_id"], package["sell_id"]):
                self.paused = True
                raise ValueError("Unrecognized protected package fill")
            identifier = f"{package_id}:{execution.con_id}"
            signed = package["quantity"] * (1 if execution.con_id == package["buy_id"] else -1)
            total = sum(
                f["signed_quantity"]
                for f in self.store.effective_fills(self.manifest.account)
                if f["order_ref"] == execution.order_ref and f["con_id"] == execution.con_id
            )
            if total * signed < 0 or abs(total) > abs(signed):
                self.paused = True
                raise ValueError("Protected package overfill or wrong direction")
            row = next(r for r in self.store.intents(self.manifest.account) if r["id"] == identifier)
            if total == signed:
                self.store.transition(identifier, "FILLED")
            elif total and row["state"] not in TERMINAL:
                self.store.transition(identifier, "PARTIALLY_FILLED")
            return
        rows = [
            r
            for r in self.store.intents(self.manifest.account)
            if r["id"] == execution.order_ref.removeprefix("hedge:")
            or (not execution.order_ref and execution.perm_id and r["perm_id"] == execution.perm_id)
        ]
        if not rows:
            return
        row = rows[0]
        fills = [
            f
            for f in self.store.effective_fills(self.manifest.account)
            if f["order_ref"] == "hedge:" + row["id"]
        ]
        quantity = sum(f["signed_quantity"] for f in fills)
        if quantity == row["signed_quantity"]:
            self.store.transition(row["id"], "FILLED")
        elif quantity and row["state"] not in TERMINAL:
            if quantity * row["signed_quantity"] < 0 or abs(quantity) > abs(row["signed_quantity"]):
                self.paused = True
                raise ValueError("Execution quantity exceeds durable intent; reconcile")
            self.store.transition(row["id"], "PARTIALLY_FILLED")

    async def reconcile(self, snapshot):
        by_ref = {o.order_ref: o for o in snapshot.orders}
        for row in self.store.intents(snapshot.account, True):
            if row["state"] in ("CREATED", "VALIDATED"):
                # A durable CREATED/VALIDATED intent was never submitted.
                self.store.transition(row["id"], "REJECTED")
            elif (
                ("hedge-package:" + row["package_id"]) if row.get("package_id") else "hedge:" + row["id"]
            ) in by_ref:
                order = by_ref[
                    ("hedge-package:" + row["package_id"]) if row.get("package_id") else "hedge:" + row["id"]
                ]
                self.on_order(
                    {
                        "order_ref": order.order_ref,
                        "order_id": order.order_id,
                        "perm_id": order.perm_id,
                        "status": "Submitted",
                        "filled": abs(row["signed_quantity"]) - abs(order.signed_remaining),
                    }
                )
            elif row["state"] in ("SUBMITTING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "CANCEL_PENDING"):
                self.store.transition(row["id"], "UNKNOWN")
        if self.unresolved():
            self.paused = True

    async def pause(self):
        self.paused = self.pause_requested = True
        requested = set()
        for row in self.store.intents(self.manifest.account, True):
            if row["state"] in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                self.store.transition(row["id"], "CANCEL_PENDING")
                if row["broker_order_id"] in requested:
                    continue
                requested.add(row["broker_order_id"])
                try:
                    await self.cancel_order(row["broker_order_id"])
                except Exception as exc:
                    self.store.record(
                        "alert",
                        self.manifest.account,
                        {"severity": "error", "message": f"Cancellation unconfirmed: {exc}"},
                    )
            elif row["state"] in ("SUBMITTING", "UNKNOWN") and row["broker_order_id"] is not None:
                if row["state"] == "SUBMITTING":
                    self.store.transition(row["id"], "UNKNOWN")
                if row["broker_order_id"] in requested:
                    continue
                requested.add(row["broker_order_id"])
                try:
                    await self.cancel_order(row["broker_order_id"])
                except Exception as exc:
                    self.store.record(
                        "alert",
                        self.manifest.account,
                        {"severity": "error", "message": f"Uncertain-order cancellation unconfirmed: {exc}"},
                    )

    def pause_complete(self):
        return self.paused and not self.store.intents(self.manifest.account, True)

    async def manage(self, quotes, now):
        seen = set()
        for row in self.store.intents(self.manifest.account, True):
            oid = row["broker_order_id"]
            if oid in seen:
                continue
            seen.add(oid)
            if row["state"] not in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                if (
                    row["state"] == "SUBMITTING"
                    and row["id"] in self.sent_at
                    and now - self.sent_at[row["id"]] > timedelta(seconds=15)
                ):
                    siblings = [
                        r
                        for r in self.store.intents(self.manifest.account, True)
                        if r["broker_order_id"] == oid
                    ]
                    for sibling in siblings:
                        self.store.transition(sibling["id"], "UNKNOWN")
                    self.paused = True
                continue
            package_id = row.get("package_id")
            sent = self.sent_at.get(row["id"])
            c = self.manifest.contract_map[row["con_id"]]
            p = self.manifest.execution
            setup = bool(package_id)
            lifetime = (
                p.setup_lifetime_seconds
                if setup
                else p.option_lifetime_seconds
                if c.kind == "OPT"
                else p.stock_lifetime_seconds
            )
            wait = (
                p.setup_wait_seconds
                if setup
                else p.option_wait_seconds
                if c.kind == "OPT"
                else p.stock_wait_seconds
            )
            try:
                if self.paused or self.pause_requested or self.broker.readonly:
                    raise ValueError("Execution paused; cancel working order before repricing")
                if sent is None or (now - sent).total_seconds() >= lifetime:
                    raise ValueError("Patient order expired; reconcile and replan")
                _, quotes, now, _ = await self.refresh_scenarios(self.current_scenarios, now)
                if self.paused or self.pause_requested or self.broker.readonly:
                    raise ValueError("Execution paused during scenario refresh")
                if (now - sent).total_seconds() >= lifetime:
                    raise ValueError("Patient order expired during scenario refresh")
                package = (
                    self.store.get_record(package_id, self.manifest.account, "setup_package")
                    if setup
                    else None
                )
                ids = [package["buy_id"], package["sell_id"]] if package else [row["con_id"]]
                for i in ids:
                    q = quotes.get(i)
                    contract = self.manifest.contract_map[i]
                    age = (
                        self.manifest.risk.option_quote_age
                        if contract.kind == "OPT"
                        else self.manifest.risk.stock_quote_age
                    )
                    if (
                        q is None
                        or q.data_type != ("synthetic" if self.gate.demo else "live")
                        or not 0 <= (now - q.observed_at).total_seconds() <= age
                        or spread_error(self.manifest, contract, q)
                    ):
                        raise ValueError("Order quote stale or spread too wide")
                elapsed = (now - sent).total_seconds()
                if not package:
                    snapshot = self.broker.snapshot()
                    current = self.gate.engine.evaluate(
                        snapshot, quotes, self.current_scenarios, snapshot.quantities()
                    )
                    urgency = urgency_for(self.manifest, c, now, not current.passed)
                if row.get("order_type") == "MKT" or (
                    elapsed < wait and (package or urgency not in ("CORRECTIVE", "DEADLINE"))
                ):
                    continue
                if package:
                    a, b = (quotes[i] for i in ids)
                    mid = a.mid - b.mid
                    spread = a.ask - a.bid + b.ask - b.bid
                    metadata = self.store.get_record(package_id, self.manifest.account, "setup_package")
                    cap = metadata["price_cap"]
                    price = rounded_price(mid + min(0.5, 0.25 * int(elapsed / wait)) * spread, 0.01, 1)
                    snapshot = self.broker.snapshot()
                    amounts = snapshot.projected()
                    if not self.gate.validate(
                        snapshot,
                        quotes,
                        self.current_scenarios,
                        amounts,
                        now,
                        2 * execution_cost(self.manifest, quotes, ids, [1, -1]),
                        day_zero=True,
                    ).passed:
                        raise ValueError("Fresh package risk invalid")
                    if price <= 0 or float(price) > cap + 1e-9:
                        raise ValueError("Package exceeds original price budget")
                    if float(price) == float(metadata["limit_price"]):
                        continue
                    self.store.record(
                        "package_amendment",
                        self.manifest.account,
                        {"package_id": package_id, "price": str(price), "at": now},
                    )
                    await self.broker.replace(oid, float(price))
                    self.store.update_package_price(package_id, self.manifest.account, price)
                else:
                    plan = self.store.execution_plan(row["id"], self.manifest.account)
                    if plan is None:
                        raise ValueError("Order has no recoverable price budget")
                    q = quotes[row["con_id"]]
                    signed = row["signed_quantity"]
                    remaining = next(
                        (
                            o.signed_remaining
                            for o in snapshot.orders
                            if o.order_id == oid and o.con_id == row["con_id"]
                        ),
                        None,
                    )
                    if remaining is None:
                        raise ValueError("Working order missing; reconcile")
                    quantities = {
                        **snapshot.quantities(),
                        row["con_id"]: snapshot.quantities().get(row["con_id"], 0) + remaining,
                    }
                    fraction = (
                        0.5 if urgency in ("CORRECTIVE", "DEADLINE") else min(0.5, 0.25 * int(elapsed / wait))
                    )
                    value = (
                        (q.ask if signed > 0 else q.bid)
                        if urgency in ("CORRECTIVE", "DEADLINE")
                        else q.mid + (1 if signed > 0 else -1) * fraction * (q.ask - q.bid)
                    )
                    tick = (
                        self.broker.price_tick(row["con_id"], value)
                        if hasattr(self.broker, "price_tick")
                        else 0.01
                    )
                    price = rounded_price(
                        value, tick, -signed if urgency in ("CORRECTIVE", "DEADLINE") else signed
                    )
                    if (float(price) - plan["price_cap"]) * (1 if signed > 0 else -1) > 1e-9:
                        raise ValueError("Order exceeds original modeled price budget")
                    costs = execution_cost(self.manifest, quotes, [row["con_id"]], [remaining])
                    if not self.gate.validate(
                        snapshot,
                        quotes,
                        self.current_scenarios,
                        quantities,
                        now,
                        2 * costs,
                        corrective=not current.passed,
                    ).passed:
                        raise ValueError("Reprice fails fresh risk check")
                    if self.recovery_checks:
                        extra = self.recovery_checks(
                            snapshot, quotes, self.current_scenarios, quantities, 2 * costs
                        )
                        if extra:
                            raise ValueError("; ".join(extra))
                    if current.passed and not self.economic_check(
                        snapshot, quotes, self.current_scenarios, quantities, now
                    ):
                        raise ValueError("Remaining order no longer improves expected score")
                    if price == row["limit_price"]:
                        continue
                    self.store.amend_limit(
                        row["id"], price, {"urgency": urgency, "quote": q.model_dump(mode="json")}
                    )
                    await self.broker.replace(oid, float(price))
            except ValueError as exc:
                for sibling in self.store.intents(self.manifest.account, True):
                    if sibling["broker_order_id"] == oid and sibling["state"] in (
                        "ACKNOWLEDGED",
                        "PARTIALLY_FILLED",
                    ):
                        self.store.transition(sibling["id"], "CANCEL_PENDING")
                self.store.record(
                    "execution_wait", self.manifest.account, {"intent_id": row["id"], "reason": str(exc)}
                )
                try:
                    await self.cancel_order(oid)
                except Exception:
                    self.paused = True
            except Exception as exc:
                self.paused = True
                self.store.record(
                    "alert",
                    self.manifest.account,
                    {"severity": "error", "message": f"Order amendment uncertain; reconcile: {exc}"},
                )
