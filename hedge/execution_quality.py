"""Execution analytics from durable facts, with corrections and unknowns preserved."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal


def timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def slippage(mid, executions, contract, valid_direction):
    if mid is None or not executions or contract is None or not valid_direction:
        return None
    return sum(
        (f["price"] - Decimal(str(mid))) * f["signed_quantity"] * contract.multiplier for f in executions
    )


def execution_quality(store, account, contracts, limit=None):
    """Per-intent execution quality; `limit` restricts it to the most recent intents.

    The bounded form reads only the related records and fills, so a dashboard poll stays cheap
    however many closing-burner legs the contest accumulates. Exports use the full form.
    """
    orders = store.intents(account, include_metadata=False, limit=limit)
    if limit is None:
        records = store.grouped_records(
            (
                "execution_plan",
                "decision",
                "order_transition",
                "price_amendment",
                "package_amendment",
                "cancel_request",
                "burn_cycle",
            ),
            account,
        )
        all_fills = store.effective_fills(account)
    else:
        ids = {o["id"] for o in orders}
        # Plans, transitions and amendments are written at or after their intent's creation.
        oldest = min((o["created_at"] for o in orders), default=None)
        since = oldest - timedelta(minutes=1) if oldest is not None else None  # clock-step margin
        records = store.grouped_records(
            ("execution_plan", "order_transition", "price_amendment"), account, intent_ids=ids, since=since
        )
        records.update(store.grouped_records(("package_amendment", "cancel_request"), account))
        records.update(
            store.grouped_records(("decision", "burn_cycle"), account, ids={o["decision_id"] for o in orders})
        )
        packages = {r["package_id"] for r in records["execution_plan"] if r.get("package_id")}
        references = {"hedge:" + i for i in ids} | {"hedge-package:" + p for p in packages}
        all_fills = store.effective_fills(account, references)
    plans = {r["intent_id"]: r for r in records["execution_plan"]}
    # Burn legs carry their cycle id as decision id; the cycle plan holds the decision midpoint.
    decisions = {r["id"]: r for r in records["decision"] + records["burn_cycle"]}
    transitions, amendments, cancels = defaultdict(list), defaultdict(int), defaultdict(int)
    package_amendments = defaultdict(int)
    for row in records["order_transition"]:
        transitions[row["intent_id"]].append(row)
    for row in records["price_amendment"]:
        amendments[row["intent_id"]] += 1
    for row in records["package_amendment"]:
        package_amendments[row["package_id"]] += 1
    for row in records["cancel_request"]:
        for identifier in row.get("intent_ids", []):
            cancels[identifier] += 1
    fills = defaultdict(list)
    for fill in all_fills:
        fills[(fill["order_ref"], fill["con_id"])].append(fill)
    result = []
    for order in orders:
        identifier, con_id = order["id"], order["con_id"]
        plan = plans.get(identifier, {})
        package = plan.get("package_id")
        reference = "hedge-package:" + package if package else "hedge:" + identifier
        executions = fills[(reference, con_id)]
        decision = decisions.get(order["decision_id"], {})
        decision_mid = decision.get("reference_midpoints", {}).get(str(con_id))
        arrival_mid = plan.get("arrival_mid")
        if arrival_mid is None and plan.get("quote"):
            arrival_mid = (Decimal(str(plan["quote"]["bid"])) + Decimal(str(plan["quote"]["ask"]))) / 2
        submitted = [timestamp(r["at"]) for r in transitions[identifier] if r["state"] == "SUBMITTING"]
        submitted_at = min(submitted) if submitted else None
        units = sum(abs(f["signed_quantity"]) for f in executions)
        sign = 1 if order["signed_quantity"] > 0 else -1
        valid_direction = all(f["signed_quantity"] * sign > 0 for f in executions)
        contract = contracts.get(con_id)

        latencies = (
            [(timestamp(f["at"]) - submitted_at).total_seconds() for f in executions] if submitted_at else []
        )
        clock_valid = bool(latencies) and min(latencies) >= 0
        known_fees = sum(
            (
                f["commission"]
                for f in executions
                if f["commission"] is not None and f["commission_currency"] == "USD"
            ),
            Decimal(0),
        )
        fees_complete = bool(executions) and all(
            f["commission"] is not None and f["commission_currency"] == "USD" for f in executions
        )
        decision_slippage = slippage(decision_mid, executions, contract, valid_direction)
        result.append(
            {
                "intent_id": identifier,
                "decision_id": order["decision_id"],
                "con_id": con_id,
                "package_id": package,
                "purpose": plan.get("purpose", "hedge"),
                "cycle_id": plan.get("cycle_id"),
                "leg": plan.get("leg"),
                "state": order["state"],
                "order_type": plan.get("order_type"),
                "requested_quantity": abs(order["signed_quantity"]),
                "filled_quantity": units,
                "submission_attempt_at": submitted_at.isoformat() if submitted_at else None,
                "fill_ratio": units / abs(order["signed_quantity"]) if submitted_at else None,
                "quantity_anomaly": not valid_direction or units > abs(order["signed_quantity"]),
                "average_fill_price": sum(f["price"] * abs(f["signed_quantity"]) for f in executions) / units
                if units
                else None,
                "first_fill_seconds": min(latencies) if clock_valid else None,
                "last_fill_seconds": max(latencies) if clock_valid else None,
                "weighted_fill_seconds": sum(
                    t * abs(f["signed_quantity"]) for t, f in zip(latencies, executions, strict=True)
                )
                / units
                if clock_valid
                else None,
                "decision_mid": decision_mid,
                "arrival_mid": arrival_mid,
                "decision_slippage_usd": decision_slippage,
                "arrival_slippage_usd": slippage(arrival_mid, executions, contract, valid_direction),
                "known_fees_usd": known_fees,
                "fees_complete": fees_complete,
                "decision_cost_usd": decision_slippage + known_fees
                if decision_slippage is not None and fees_complete
                else None,
                "amendment_attempts": package_amendments[package] if package else amendments[identifier],
                "cancel_attempts": cancels[identifier],
            }
        )
    return result
