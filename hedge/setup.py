"""Plan mandatory options and their structural hedges before any purchase."""

from itertools import product

import numpy as np

from .models import Target
from .optimizer import execution_cost
from .order_policy import spread_error


class SetupPlanner:
    def __init__(self, manifest, gate):
        self.manifest, self.gate = manifest, gate

    def plan(self, snapshot, quotes, scenarios, now):
        m = self.manifest
        if m.contest_rules == "spcx_rklb_spy":
            raise ValueError(
                "Automatic contest setup is unsupported: seven-leg signed mandates require "
                "a validated package adapter; reconcile the complete mandated portfolio before hedging"
            )
        if len(m.mandatory) > 8:
            raise ValueError("Protected setup requires a validated adapter for more than eight mandate legs")
        actual = snapshot.quantities()
        choices = []
        for i in sorted(m.mandatory):
            c = m.contract_map[i]
            if c.kind != "OPT" or c.multiplier != 100 or c.currency != "USD":
                raise ValueError("Protected setup requires validated standard US long-option mandates")
            if i not in quotes or spread_error(m, c, quotes[i]):
                raise ValueError(f"Protected setup waiting for a narrow live mandate quote: {c.label}")
            eligible = [
                h
                for h in m.contracts
                if h.con_id in m.allowlist
                and h.kind == "OPT"
                and h.underlying_id == c.underlying_id
                and h.expiry == c.expiry
                and h.right == c.right
                and h.multiplier == c.multiplier
                and h.currency == c.currency
                and h.trading_class == c.trading_class
                and ((h.strike > c.strike) if c.right == "C" else (h.strike < c.strike))
                and h.con_id in quotes
                and not spread_error(m, h, quotes[h.con_id])
            ]
            eligible.sort(key=lambda h: (abs(h.strike - c.strike), h.con_id))
            if not eligible:
                raise ValueError(
                    f"No approved protective vertical hedge for {c.label}; no unhedged purchase allowed"
                )
            choices.append([(i, h.con_id) for h in eligible[:4]])
        if np.prod([len(x) for x in choices]) > 256:
            raise ValueError("Protected setup template space exceeds validated bound")
        candidates = []
        for pairs in product(*choices):
            if len({h for _, h in pairs}) != len(pairs):
                continue
            target = {**actual, **m.mandatory}
            for i, h in pairs:
                target[h] = -m.mandatory[i]
            for c in m.contracts:
                if c.kind != "STK" or c.con_id not in m.allowlist:
                    continue
                delta = sum(
                    qty * m.contract_map[i].multiplier * quotes[i].delta
                    for i, qty in target.items()
                    if m.contract_map[i].kind == "OPT" and m.contract_map[i].underlying_id == c.con_id
                )
                if not any(m.contract_map[i].underlying_id == c.con_id for i in m.mandatory):
                    continue
                hedge = -round(delta)
                if hedge < 0:
                    available = max(quotes[c.con_id].shortable_shares or 0, max(0, -actual.get(c.con_id, 0)))
                    hedge = -min(abs(hedge), available)
                if abs(hedge) * quotes[c.con_id].spot < m.execution.minimum_stock_notional:
                    hedge = 0
                target[c.con_id] = hedge
            ids = list(target)
            delta = [target[i] - actual.get(i, 0) for i in ids]
            cost = execution_cost(m, quotes, ids, delta)
            risk = self.gate.validate(snapshot, quotes, scenarios, target, now, 2 * cost, day_zero=True)
            if not risk.passed:
                continue
            # Validate an executable hedge-first, balanced paired acquisition
            # path rather than requiring irrelevant, unbalanced fill corners.
            prepared = dict(target)
            for i, h in pairs:
                prepared[i] = actual.get(i, 0)
                prepared[h] = -actual.get(i, 0)
            if not self.gate.validate(
                snapshot, quotes, scenarios, prepared, now, 2 * cost, day_zero=True
            ).passed:
                continue
            simulated = dict(prepared)
            path = []
            while any(simulated.get(i, 0) < m.mandatory[i] for i, h in pairs):
                steps = []
                for i, h in pairs:
                    if simulated.get(i, 0) >= m.mandatory[i]:
                        continue
                    amounts = {**simulated, i: simulated.get(i, 0) + 1, h: simulated.get(h, 0) - 1}
                    check = self.gate.validate(
                        snapshot, quotes, scenarios, amounts, now, 2 * cost, day_zero=True
                    )
                    if check.passed:
                        steps.append((check.metrics["stress_loss"], i, h, amounts))
                if not steps:
                    break
                _, i, h, simulated = min(steps, key=lambda step: step[:3])
                path.append((i, h))
            if any(simulated.get(i, 0) != m.mandatory[i] for i, h in pairs):
                continue
            vector = np.array([target.get(i, 0) for i in scenarios.ids])
            score = float(scenarios.weights @ np.abs(scenarios.changes @ vector))
            candidates.append((score, cost, tuple(pairs), target, risk, path))
        if not candidates:
            raise ValueError("No complete mandatory-plus-hedge setup passes independent risk checks")
        optimum = min(x[0] for x in candidates)
        score, cost, pairs, quantities, risk, path = min(
            (x for x in candidates if x[0] <= optimum + 1), key=lambda x: (x[1], x[2])
        )
        return {
            "target": Target(
                quantities={i: q for i, q in quantities.items() if q},
                expected_score=score,
                current_score=0,
                uncertainty=0,
                status="PROTECTED_SETUP",
                snapshot_id=snapshot.version,
                scenario_id=scenarios.identifier,
                config_hash=m.config_hash,
                estimated_cost=cost,
                explanation="Complete protected-option and residual-stock Day 0 plan; guaranteed pairs only",
            ),
            "pairs": dict(pairs),
            "acquisition_path": path,
            "risk": risk,
        }
