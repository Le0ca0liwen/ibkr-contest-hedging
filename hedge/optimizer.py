from __future__ import annotations

from time import monotonic

import cvxpy as cp
import numpy as np

from .cashflows import cash_carry
from .config import NY
from .models import Target
from .order_policy import spread_error


def execution_cost(manifest, quotes, identifiers, delta):
    """Fixed, executable slice model; no freely adjustable cost variables."""
    cost = 0.0
    for i, d in zip(identifiers, delta, strict=True):
        if not d:
            continue
        c, q = manifest.contract_map[i], quotes[i]
        available = q.ask_size if d > 0 else q.bid_size
        size = max(
            1,
            min(
                manifest.execution.option_slice if c.kind == "OPT" else manifest.execution.stock_slice,
                available,
            ),
        )
        rate = manifest.policy.commission_option if c.kind == "OPT" else manifest.policy.commission_share
        # All full slices have the same modeled fee; final partial slice is
        # conservatively charged a full slice. Actual fees always come from IBKR.
        fee = max(manifest.policy.minimum_commission, rate * size)
        spread = (q.ask - q.bid) * (0.5 + manifest.policy.slippage_spread_fraction) * c.multiplier
        cost += abs(d) * spread + np.ceil(abs(d) / size) * fee
    return float(cost)


def _positive_part(expression, lower, upper, constraints):
    """Exact max(expression, 0), with no binary when its sign is proved."""
    if lower >= 0:
        return expression
    if upper <= 0:
        return cp.Constant(0)
    positive, negative = cp.Variable(nonneg=True), cp.Variable(nonneg=True)
    sign = cp.Variable(boolean=True)
    constraints.extend(
        [
            positive - negative == expression,
            positive <= upper * sign,
            negative <= -lower * (1 - sign),
        ]
    )
    return positive


def _stress_projection(stress, idx, fixed):
    """Keep the strongest row for identical variable coefficients, exactly.

    Frozen option IV/skew shocks often repeat the same stock coefficients.
    Their lowest fixed portfolio value dominates every other row in that group.
    No rounding, stress sampling, or approximate dominance is used.
    """
    matrix, inverse = np.unique(stress[:, idx], axis=0, return_inverse=True)
    offset = np.full(len(matrix), np.inf)
    np.minimum.at(offset, inverse, fixed)
    return matrix, offset


class HedgeOptimizer:
    def __init__(self, manifest):
        self.manifest = manifest
        self.baseline_nav = float(manifest.initial_nav)

    def evaluate_target(
        self, snapshot, quotes, scenarios, target, previous_nav, nav_estimate, include_costs=True
    ):
        """Revalidate a remaining execution plan against fresh marks and paid costs."""
        m = self.manifest
        actual = snapshot.quantities()
        ids = scenarios.ids
        current = np.array([actual.get(i, 0) for i in ids])
        desired = np.array([target.quantities.get(i, 0) for i in ids])
        delta = desired - current
        costs = execution_cost(m, quotes, ids, delta) if include_costs else 0
        days = scenarios.calendar_days

        def carry(quantities, cash):
            borrow = sum(
                max(0, -amount) * quotes[i].spot
                for i, amount in zip(ids, quantities, strict=True)
                if m.contract_map[i].kind == "STK"
            )
            return cash_carry(m, cash, borrow, days, float(snapshot.nav))

        cash = float(snapshot.cash)
        planned_cash = (
            cash
            - sum(
                amount * quotes[i].mid * m.contract_map[i].multiplier
                for i, amount in zip(ids, delta, strict=True)
            )
            - costs
        )
        unscored_setup = snapshot.at.astimezone(NY).date() == m.day_zero
        error = 0 if unscored_setup else nav_estimate - float(previous_nav)
        old = error + scenarios.changes @ current + carry(current, cash)
        new = (
            error
            + scenarios.changes @ desired
            + carry(desired, planned_cash)
            - (0 if unscored_setup else costs)
        )
        improvements = np.abs(old) - np.abs(new)
        uncertainty = (
            float(np.std(improvements, ddof=1) / np.sqrt(len(improvements))) if len(improvements) > 1 else 0
        )
        return target.model_copy(
            update={
                "current_score": float(scenarios.weights @ np.abs(old)),
                "expected_score": float(scenarios.weights @ np.abs(new)),
                "estimated_cost": costs,
                "uncertainty": uncertainty,
                "snapshot_id": snapshot.version,
                "scenario_id": scenarios.identifier,
            }
        )

    def solve(
        self,
        snapshot,
        quotes,
        scenarios,
        previous_nav,
        nav_estimate=None,
        stock_only=False,
        strategy=None,
        recovery_reference=None,
        recovery_symbols=(),
        recovery_stress=0.0025,
        recovery_gross=0.05,
    ):
        started = monotonic()
        metrics = {"stages": []}
        result = self._solve(
            snapshot,
            quotes,
            scenarios,
            previous_nav,
            nav_estimate,
            stock_only,
            strategy,
            recovery_reference,
            recovery_symbols,
            recovery_stress,
            recovery_gross,
            metrics,
            started + self.manifest.policy.solver_seconds,
        )
        metrics["total_ms"] = round((monotonic() - started) * 1000, 3)
        return result.model_copy(update={"solver_metrics": metrics})

    def _solve(
        self,
        snapshot,
        quotes,
        scenarios,
        previous_nav,
        nav_estimate,
        stock_only,
        strategy,
        recovery_reference,
        recovery_symbols,
        recovery_stress,
        recovery_gross,
        metrics,
        deadline,
    ):
        m = self.manifest
        strategy = strategy or m.strategy
        actual = snapshot.quantities()
        ids = scenarios.ids
        budget = min(self.baseline_nav, float(snapshot.nav))
        if budget <= 0:
            raise ValueError("Nonpositive NAV")
        quantities = np.array([actual.get(i, 0) for i in ids], dtype=float)
        unscored_setup = snapshot.at.astimezone(NY).date() == m.day_zero
        error = (
            0
            if unscored_setup
            else (nav_estimate if nav_estimate is not None else float(snapshot.nav)) - float(previous_nav)
        )
        candidates = [
            i for i in ids if i in m.allowlist and (not stock_only or m.contract_map[i].kind == "STK")
        ]
        allowed_type = "synthetic" if snapshot.source == "synthetic" else "live"
        candidates = [
            i
            for i in candidates
            if quotes[i].data_type == allowed_type
            and not spread_error(m, m.contract_map[i], quotes[i])
            and -1
            <= (snapshot.at - quotes[i].observed_at).total_seconds()
            <= (m.risk.option_quote_age if m.contract_map[i].kind == "OPT" else m.risk.stock_quote_age)
            and (
                m.contract_map[i].kind == "STK"
                or (
                    quotes[i].iv is not None
                    and quotes[i].greek_at is not None
                    and -1 <= (snapshot.at - quotes[i].greek_at).total_seconds() <= m.risk.greek_age
                )
            )
        ]
        frozen = [i for i in ids if i not in candidates]
        metrics["candidate_count"] = len(candidates)
        if not candidates:
            return self._unchanged(snapshot, scenarios, error, quotes, "NO_CANDIDATES")
        idx = [ids.index(i) for i in candidates]
        current = quantities[idx]
        n = len(candidates)
        bounds = np.array(
            [
                m.risk.contracts_per_option
                if m.contract_map[i].kind == "OPT"
                else max(1, int(m.risk.stock_notional * budget / quotes[i].spot))
                for i in candidates
            ]
        )
        lower, upper = -bounds.copy(), bounds.copy()
        for j, i in enumerate(candidates):
            c, q = m.contract_map[i], quotes[i]
            if c.kind != "STK":
                continue
            lower[j] = max(lower[j], -max(q.shortable_shares or 0, max(0, -actual.get(i, 0))))
            # A symbol with one variable stock and no variable options has an
            # exact affine delta interval. Use its implied integer bounds.
            same_symbol = [k for k in candidates if m.contract_map[k].symbol == c.symbol]
            coefficient = q.delta * q.spot
            if same_symbol == [i] and coefficient:
                fixed_delta = sum(
                    quotes[k].delta * quotes[k].spot * m.contract_map[k].multiplier * actual.get(k, 0)
                    for k in frozen
                    if m.contract_map[k].symbol == c.symbol
                )
                endpoints = sorted(
                    (
                        (-m.risk.residual_delta * budget - fixed_delta) / coefficient,
                        (m.risk.residual_delta * budget - fixed_delta) / coefficient,
                    )
                )
                lower[j] = max(lower[j], np.ceil(endpoints[0] - 1e-9))
                upper[j] = min(upper[j], np.floor(endpoints[1] + 1e-9))
        if np.any(lower > upper):
            return self._unchanged(snapshot, scenarios, error, quotes, "INFEASIBLE")
        buy_bounds, sell_bounds = np.maximum(upper - current, 0), np.maximum(current - lower, 0)
        # Only net holdings need integer domains. Exact sign/direction gates
        # imply integrality of the positive/negative and buy/sell auxiliaries.
        h = cp.Variable(n, integer=True)
        hpos, hneg = cp.Variable(n, nonneg=True), cp.Variable(n, nonneg=True)
        buy, sell = cp.Variable(n, nonneg=True), cp.Variable(n, nonneg=True)
        buying = cp.hstack([cp.Variable(boolean=True) if b > 0 else 0 for b in buy_bounds])
        selling = cp.hstack([cp.Variable(boolean=True) if b > 0 else 0 for b in sell_bounds])
        sign = cp.hstack(
            [
                1
                if lower[j] >= 0
                else 0
                if upper[j] <= 0
                else buying[j]
                if current[j] == 0
                else cp.Variable(boolean=True)
                for j in range(n)
            ]
        )
        selected = cp.hstack(
            [
                1
                if lower[j] > 0 or upper[j] < 0
                else buying[j] + selling[j]
                if current[j] == 0
                else cp.Variable(boolean=True)
                for j in range(n)
            ]
        )
        ah = hpos + hneg
        constraints = [
            h == hpos - hneg,
            h >= lower,
            h <= upper,
            hpos <= cp.multiply(np.maximum(upper, 0), sign),
            hneg <= cp.multiply(np.maximum(-lower, 0), 1 - sign),
            ah <= cp.multiply(bounds, selected),
            ah >= selected,
            buy <= cp.multiply(buy_bounds, buying),
            sell <= cp.multiply(sell_bounds, selling),
            buy >= buying,
            sell >= selling,
            buying + selling <= 1,
            h - current == buy - sell,
        ]
        for j, i in enumerate(candidates):
            if m.contract_map[i].kind == "STK":
                borrow_limit = quotes[i].shortable_shares or 0
                constraints += [hneg[j] <= max(borrow_limit, max(0, -actual.get(i, 0)))]
        from .risk import RiskEngine

        correcting = not RiskEngine(m).evaluate(snapshot, quotes, scenarios, actual).passed
        if not correcting:
            for j, i in enumerate(candidates):
                if m.contract_map[i].kind == "STK":
                    minimum = max(1, int(np.ceil(m.execution.minimum_stock_notional / quotes[i].mid)))
                    constraints += [buy[j] >= minimum * buying[j], sell[j] >= minimum * selling[j]]
        buy_sizes, sell_sizes, fees_buy, fees_sell, spread_cost = [], [], [], [], []
        for i in candidates:
            c, q = m.contract_map[i], quotes[i]
            buy_size = max(
                1, min(m.execution.option_slice if c.kind == "OPT" else m.execution.stock_slice, q.ask_size)
            )
            sell_size = max(
                1, min(m.execution.option_slice if c.kind == "OPT" else m.execution.stock_slice, q.bid_size)
            )
            rate = m.policy.commission_option if c.kind == "OPT" else m.policy.commission_share
            buy_sizes.append(buy_size)
            sell_sizes.append(sell_size)
            fees_buy.append(max(m.policy.minimum_commission, rate * buy_size))
            fees_sell.append(max(m.policy.minimum_commission, rate * sell_size))
            spread_cost.append((q.ask - q.bid) * (0.5 + m.policy.slippage_spread_fraction) * c.multiplier)

        def slice_count(amount, size, limit, active):
            if size == 1:
                return amount
            if limit == 0:
                return cp.Constant(0)
            count = cp.Variable(integer=True)
            constraints.extend(
                [
                    count >= active,
                    count <= np.ceil(limit / size) * active,
                    size * count >= amount,
                    # The remainder allowance only exists for an active trade.
                    # This eliminates phantom fractional commissions from the
                    # LP relaxation without changing any integer solution.
                    size * count <= amount + (size - 1) * active,
                ]
            )
            return count

        # Equal buy/sell slice schedules share one exact count. Asymmetric
        # displayed liquidity still uses direction-specific counts and fees.
        commissions = []
        for j in range(n):
            if buy_sizes[j] == sell_sizes[j] and fees_buy[j] == fees_sell[j]:
                count = slice_count(
                    buy[j] + sell[j],
                    buy_sizes[j],
                    max(buy_bounds[j], sell_bounds[j]),
                    buying[j] + selling[j],
                )
                commissions.append(fees_buy[j] * count)
            else:
                buy_count = slice_count(buy[j], buy_sizes[j], buy_bounds[j], buying[j])
                sell_count = slice_count(sell[j], sell_sizes[j], sell_bounds[j], selling[j])
                commissions.append(fees_buy[j] * buy_count + fees_sell[j] * sell_count)
        commissions = cp.hstack(commissions)
        costs = np.array(spread_cost) @ (buy + sell) + cp.sum(commissions)
        all_h = [h[candidates.index(i)] if i in candidates else actual.get(i, 0) for i in ids]
        total_h = cp.hstack(all_h)
        absolute_h = cp.hstack(
            [ah[candidates.index(i)] if i in candidates else abs(actual.get(i, 0)) for i in ids]
        )
        # Cash sign is exact, so financing is not a fictitious adjustable loss.
        prices = np.array([quotes[i].mid * m.contract_map[i].multiplier for i in candidates])
        cash_after = float(snapshot.cash) - prices @ (h - current) - costs
        cash_bound = 10 * budget
        cost_bound = float(
            np.array(spread_cost) @ np.maximum(buy_bounds, sell_bounds)
            + np.sum(
                np.maximum(
                    np.array(fees_buy) * np.ceil(buy_bounds / np.array(buy_sizes)),
                    np.array(fees_sell) * np.ceil(sell_bounds / np.array(sell_sizes)),
                )
            )
        )
        cash_lower = max(-cash_bound, float(snapshot.cash) + prices @ (current - upper) - cost_bound)
        cash_upper = min(cash_bound, float(snapshot.cash) + prices @ (current - lower))
        constraints += [cash_after >= -cash_bound, cash_after <= cash_bound]
        cash_minus = _positive_part(-cash_after, -cash_upper, -cash_lower, constraints)
        days = scenarios.calendar_days
        short_stock = (
            cp.sum(
                cp.hstack(
                    [
                        hneg[j] * quotes[i].spot
                        for j, i in enumerate(candidates)
                        if m.contract_map[i].kind == "STK"
                    ]
                )
            )
            if any(m.contract_map[i].kind == "STK" for i in candidates)
            else 0
        )
        frozen_short = sum(
            max(0, -actual.get(i, 0)) * quotes[i].spot for i in frozen if m.contract_map[i].kind == "STK"
        )
        # Exact positive eligible-cash split: cannot invent interest to fit NAV.
        eligible_cash = cash_after - 1.02 * (short_stock + frozen_short) - m.policy.cash_interest_free_balance
        eligible_bound = 20 * budget + m.policy.cash_interest_free_balance
        constraints += [eligible_cash >= -eligible_bound, eligible_cash <= eligible_bound]
        short_bound = frozen_short + sum(
            max(0, -lower[j]) * quotes[i].spot
            for j, i in enumerate(candidates)
            if m.contract_map[i].kind == "STK"
        )
        eligible_lower = max(
            -eligible_bound, cash_lower - 1.02 * short_bound - m.policy.cash_interest_free_balance
        )
        eligible_upper = min(
            eligible_bound, cash_upper - 1.02 * frozen_short - m.policy.cash_interest_free_balance
        )
        eligible_plus = (
            _positive_part(eligible_cash, eligible_lower, eligible_upper, constraints)
            if m.policy.cash_rate
            else cp.Constant(0)
        )
        carry = (
            days
            / m.policy.cash_day_count
            * (
                m.policy.cash_rate * min(1, float(snapshot.nav) / 100000) * eligible_plus
                - m.policy.financing_rate * cash_minus
                - m.policy.borrow_rate * (short_stock + frozen_short)
            )
        )
        scored_costs = 0 if unscored_setup else costs
        pnl = error + scenarios.changes @ total_h + carry - scored_costs
        objective_score = scenarios.weights @ cp.abs(pnl)
        if strategy == "historical_l1":
            objective_score = scenarios.weights @ cp.abs(scenarios.changes @ total_h + carry - costs)
        if strategy == "two_close" and scenarios.next_changes is not None:
            objective_score += scenarios.weights @ cp.abs(scenarios.next_changes @ total_h)
        fixed_quantities = quantities.copy()
        fixed_quantities[idx] = 0
        stress_matrix, stress_fixed = _stress_projection(
            scenarios.stress, idx, scenarios.stress @ fixed_quantities
        )
        metrics.update({"stress_rows_input": len(scenarios.stress), "stress_rows_model": len(stress_matrix)})
        constraints += [stress_matrix @ h + stress_fixed - 2 * costs >= -m.risk.stress_loss * budget]
        if recovery_reference is not None:
            if not 0 < recovery_stress <= 0.0025 or not 0 < recovery_gross <= 0.05:
                raise ValueError("Recovery budget cannot exceed validated bounds")
            ref = np.array([recovery_reference.get(i, 0) for i in ids])
            recovery_j = [
                j
                for j, i in enumerate(candidates)
                if m.contract_map[i].kind == "STK" and m.contract_map[i].symbol in recovery_symbols
            ]
            for j, i in enumerate(ids):
                if m.contract_map[i].kind != "STK" or m.contract_map[i].symbol not in recovery_symbols:
                    constraints.append(total_h[j] == ref[j])
            recovery_costs = (
                (
                    np.array(spread_cost)[recovery_j] @ (buy[recovery_j] + sell[recovery_j])
                    + cp.sum(commissions[recovery_j])
                )
                if recovery_j
                else 0
            )
            recovery_matrix, recovery_fixed = _stress_projection(
                scenarios.stress, idx, scenarios.stress @ (fixed_quantities - ref)
            )
            constraints += [
                recovery_matrix @ h + recovery_fixed - 2 * recovery_costs >= -recovery_stress * budget
            ]
            constraints += [
                cp.sum(
                    cp.hstack(
                        [
                            quotes[i].spot * cp.abs(total_h[j] - ref[j])
                            for j, i in enumerate(ids)
                            if m.contract_map[i].kind == "STK"
                            and m.contract_map[i].symbol in recovery_symbols
                        ]
                    )
                )
                <= recovery_gross * budget
            ]

        spots = np.array([quotes[i].spot * m.contract_map[i].multiplier for i in ids])
        constraints += [spots @ absolute_h <= m.risk.gross_notional * budget]
        increment = cp.Variable(n, nonneg=True)
        constraints += [increment >= ah - np.abs(current)]
        margin_rates = np.array(
            [
                (quotes[i].mid + 0.2 * quotes[i].spot) * m.contract_map[i].multiplier
                if m.contract_map[i].kind == "OPT"
                else 0.5 * quotes[i].spot
                for i in candidates
            ]
        )
        constraints += [
            margin_rates @ increment + costs
            <= float(snapshot.excess_liquidity) - m.risk.liquidity_buffer * budget
        ]
        symbols = sorted({m.contract_map[i].symbol for i in ids})
        delta_vectors = {}
        for symbol in symbols:
            delta_vector = np.array(
                [
                    quotes[i].delta * quotes[i].spot * m.contract_map[i].multiplier
                    if m.contract_map[i].symbol == symbol
                    else 0
                    for i in ids
                ]
            )
            delta_vectors[symbol] = delta_vector
            constraints += [cp.abs(delta_vector @ total_h) <= m.risk.residual_delta * budget]
            stock_vector = np.array(
                [
                    quotes[i].spot
                    if m.contract_map[i].symbol == symbol and m.contract_map[i].kind == "STK"
                    else 0
                    for i in ids
                ]
            )
            constraints += [stock_vector @ absolute_h <= m.risk.stock_notional * budget]
            option_j = [
                j
                for j, i in enumerate(candidates)
                if m.contract_map[i].symbol == symbol and m.contract_map[i].kind == "OPT"
            ]
            frozen_names = sum(
                actual.get(i, 0) != 0
                for i in frozen
                if i not in m.mandatory
                and m.contract_map[i].symbol == symbol
                and m.contract_map[i].kind == "OPT"
            )
            if option_j:
                constraints += [cp.sum(selected[option_j]) + frozen_names <= m.risk.option_names]
        # Screening baselines choose numerical quantities, still using same hard constraints.
        baseline = None
        baseline_vectors = []
        if strategy == "delta":
            baseline = cp.sum(cp.hstack([cp.abs(v @ total_h) for v in delta_vectors.values()]))
        elif strategy == "greek":
            rows = []
            for symbol in symbols:
                # Dollar sensitivities to 1% spot/IV/rate changes and one day
                # of decay; the main score objective already fully reprices options.
                for greek, scale in [
                    ("delta", 0.01),
                    ("gamma", 0.00005),
                    ("vega", 0.01),
                    ("theta", 1),
                    ("rho", 0.01),
                ]:
                    vector = np.array(
                        [
                            (
                                scenarios.rhos.get(i, quotes[i].rho or 0)
                                if greek == "rho"
                                else getattr(quotes[i], greek)
                            )
                            * m.contract_map[i].multiplier
                            * (
                                quotes[i].spot * scale
                                if greek == "delta"
                                else (quotes[i].spot ** 2 * scale if greek == "gamma" else scale)
                            )
                            if m.contract_map[i].symbol == symbol
                            else 0
                            for i in ids
                        ]
                    )
                    rows.append(cp.abs(vector @ total_h))
                    baseline_vectors.append(vector)
            if all(m.contract_map[i].symbol in scenarios.betas for i in ids):
                vector = np.array(
                    [
                        quotes[i].delta
                        * quotes[i].spot
                        * m.contract_map[i].multiplier
                        * scenarios.betas[m.contract_map[i].symbol]
                        * 0.01
                        for i in ids
                    ]
                )
                rows.append(cp.abs(vector @ total_h))
                baseline_vectors.append(vector)
            baseline = cp.sum(cp.hstack(rows))
        if strategy == "min_variance":
            # QP baseline supplies a desired continuous target, then integer L1
            # projection enforces identical trade/risk constraints.
            continuous = cp.Variable(n)
            fixed = scenarios.changes @ quantities - scenarios.changes[:, idx] @ current
            matrix = scenarios.changes[:, idx]
            centered = matrix - matrix.mean(axis=0)
            covariance = centered.T @ centered / max(1, len(matrix) - 1)
            covariance += np.eye(n) * max(1e-6, np.trace(covariance) / max(n, 1) * 0.01)
            problem = cp.Problem(
                cp.Minimize(
                    cp.quad_form(continuous, cp.psd_wrap(covariance))
                    + 2 * (centered.T @ (fixed - fixed.mean()) / max(1, len(matrix) - 1)) @ continuous
                ),
                [continuous <= bounds, continuous >= -bounds],
            )
            problem.solve(solver=cp.OSQP, max_iter=10000)
            if problem.status != "optimal":
                return self._unchanged(snapshot, scenarios, error, quotes, "BASELINE_FAILED")
            baseline = cp.norm1(h - continuous.value)
        options = {
            "threads": 1,
            "random_seed": m.policy.seed,
            "time_limit": m.policy.solver_seconds,
            "mip_rel_gap": 0.0,
            "mip_abs_gap": 1e-6,
        }
        primary = baseline if baseline is not None else objective_score
        # One parameterized model for all lexicographic stages. CVXPY reuses
        # canonicalization, and HiGHS receives the previous feasible incumbent
        # through its supported warm-start interface (unchanged column order).
        max_holdings = np.abs(quantities)
        max_holdings[idx] = np.maximum(np.abs(lower), np.abs(upper))
        carry_bound = (
            days
            / m.policy.cash_day_count
            * (
                m.policy.cash_rate * eligible_bound
                + m.policy.financing_rate * cash_bound
                + m.policy.borrow_rate * short_bound
            )
        )
        next_objective = (
            scenarios.weights @ cp.abs(scenarios.next_changes @ total_h)
            if baseline is None and scenarios.next_changes is not None
            else None
        )
        next_bound = (
            float(np.max(np.abs(scenarios.next_changes) @ max_holdings))
            if scenarios.next_changes is not None
            else 0.0
        )
        primary_bound = abs(error) + float(np.max(np.abs(scenarios.changes) @ max_holdings))
        primary_bound += carry_bound + cost_bound
        if strategy == "two_close":
            primary_bound += next_bound
        if strategy == "delta":
            primary_bound = sum(np.abs(v) @ max_holdings for v in delta_vectors.values())
        elif strategy == "greek":
            primary_bound = sum(np.abs(v) @ max_holdings for v in baseline_vectors)
        elif strategy == "min_variance":
            primary_bound = np.sum(
                np.maximum(np.abs(lower - continuous.value), np.abs(upper - continuous.value))
            )
        weights = cp.Parameter(3, nonneg=True, value=[1, 0, 0])
        primary_limit = cp.Parameter(nonneg=True, value=float(primary_bound + max(1, primary_bound * 1e-9)))
        primary_epigraph = cp.Variable(nonneg=True)
        constraints += [primary_epigraph >= primary, primary_epigraph <= primary_limit]
        next_epigraph = cp.Variable(nonneg=True) if next_objective is not None else cp.Constant(0)
        if next_objective is not None:
            next_limit = cp.Parameter(nonneg=True, value=next_bound + max(1, next_bound * 1e-9))
            constraints += [next_epigraph >= next_objective, next_epigraph <= next_limit]
        # Original turnover/cardinality/stable-ID preference, unchanged.
        tie = prices @ (buy + sell)
        tie += cp.sum(selected) * 0.001 + np.arange(1, n + 1) @ h * 1e-9
        problem = cp.Problem(
            cp.Minimize(weights[0] * primary_epigraph + weights[1] * next_epigraph + weights[2] * tie),
            constraints,
        )
        metrics["integer_variables"] = sum(
            v.size for v in problem.variables() if v.attributes["integer"] or v.attributes["boolean"]
        )
        metrics["scalar_variables"] = problem.size_metrics.num_scalar_variables

        accepted = {}

        def stage(name, weight, absolute_gap, stage_budget=None):
            started = monotonic()
            weights.value = weight
            # Canonicalize before computing the remaining solver budget.
            problem.get_problem_data(cp.HIGHS, enforce_dpp=True)
            remaining = deadline - monotonic()
            if stage_budget is not None:
                remaining = min(remaining, stage_budget)
            if remaining <= 0:
                metrics["stages"].append({"stage": name, "status": "TIMEOUT"})
                return False
            options["time_limit"] = remaining
            options["mip_abs_gap"] = absolute_gap
            if stage_budget is not None:
                options["mip_max_nodes"] = m.policy.ranking_nodes
            problem.solve(solver=cp.HIGHS, warm_start=True, enforce_dpp=True, highs_options=options)
            info = problem.solver_stats.extra_stats
            gap, bound = info.mip_gap, info.mip_dual_bound
            metrics["stages"].append(
                {
                    "stage": name,
                    "status": problem.status,
                    "total_ms": round((monotonic() - started) * 1000, 3),
                    "solver_ms": round(problem.solver_stats.solve_time * 1000, 3),
                    "nodes": info.mip_node_count,
                    "mip_gap": float(gap) if np.isfinite(gap) else None,
                    "dual_bound": float(bound) if np.isfinite(bound) else None,
                    "incumbent": float(problem.value) if np.isfinite(problem.value) else None,
                    "absolute_gap_limit": absolute_gap,
                }
            )
            valid = (
                problem.status == "optimal"
                and np.isfinite(bound)
                # HiGHS excludes CVXPY's constant objective offset. Compare
                # its own primal/dual values, especially for cardinality ties.
                and info.objective_function_value - bound <= absolute_gap + 1e-5
            )
            if valid:
                accepted.update(
                    {
                        "stage": name,
                        "holdings": np.array(h.value, copy=True),
                        "primary": float(primary.value),
                        "next": float(next_objective.value) if next_objective is not None else None,
                    }
                )
            return valid

        try:
            primary_band = 1 if baseline is None else 0.01
            if not stage("primary", [1, 0, 0], primary_band / 4):
                return self._unchanged(
                    snapshot,
                    scenarios,
                    error,
                    quotes,
                    "OBJECTIVE_CERTIFICATE_FAILED"
                    if problem.status == "optimal"
                    else (problem.status or "TIMEOUT").upper(),
                )
            optimum = float(primary.value)
            # A certified lower bound, not the incumbent, anchors the original
            # $1 selection band. Every final portfolio is therefore <= true
            # global optimum + $1 even when the first stage stops with a small
            # absolute gap. Baselines retain their original 0.01 band.
            primary_bound = float(problem.solver_stats.extra_stats.mip_dual_bound)
            primary_limit.value = primary_bound + primary_band
            metrics.update(
                {
                    "primary_incumbent": optimum,
                    "primary_lower_bound": primary_bound,
                    "primary_selection_limit": float(primary_limit.value),
                }
            )
            ranking_started = monotonic()
            next_ranked = next_objective is None
            if next_objective is not None:
                if stage("next_close", [0, 1, 0], 0.25, m.policy.ranking_seconds / 2):
                    next_ranked = True
                    next_limit.value = float(problem.solver_stats.extra_stats.mip_dual_bound) + 1
                    metrics["next_selection_limit"] = float(next_limit.value)
            ranking_remaining = m.policy.ranking_seconds - (monotonic() - ranking_started)
            if next_ranked and ranking_remaining > 0:
                stage("turnover", [0, 0, 1], 1e-6, ranking_remaining)
            metrics["accepted_stage"] = accepted["stage"]
            metrics["turnover_complete"] = accepted["stage"] == "turnover"
            metrics["ranking_complete"] = metrics["turnover_complete"] and (
                next_objective is None or "next_selection_limit" in metrics
            )
            if monotonic() > deadline + 0.5:
                return self._unchanged(snapshot, scenarios, error, quotes, "TIMEOUT")
            # A timed-out ranking never replaces the saved certified solution.
            if accepted["primary"] > primary_limit.value + 1e-5 or (
                "next_selection_limit" in metrics
                and accepted["next"] > metrics["next_selection_limit"] + 1e-5
            ):
                return self._unchanged(snapshot, scenarios, error, quotes, "OBJECTIVE_CERTIFICATE_FAILED")
            result = dict(actual)
            for i, value in zip(candidates, accepted["holdings"], strict=True):
                result[i] = int(round(value))
            result = {i: q for i, q in result.items() if q}
            delta = [result.get(i, 0) - actual.get(i, 0) for i in candidates]
            cost = execution_cost(m, quotes, candidates, delta)
            target_vector = np.array([result.get(i, 0) for i in ids])
            planned_cash = float(snapshot.cash) - prices @ (target_vector[idx] - current) - cost
            target_carry = cash_carry(
                m,
                planned_cash,
                sum(
                    max(0, -result.get(i, 0)) * quotes[i].spot for i in ids if m.contract_map[i].kind == "STK"
                ),
                days,
                float(snapshot.nav),
            )
            new_pnl = (
                error + scenarios.changes @ target_vector + target_carry - (0 if unscored_setup else cost)
            )
            numerical_primary = float(scenarios.weights @ np.abs(new_pnl))
            if strategy == "historical_l1":
                numerical_primary = float(
                    scenarios.weights @ np.abs(scenarios.changes @ target_vector + target_carry - cost)
                )
            elif strategy == "two_close" and scenarios.next_changes is not None:
                numerical_primary += float(scenarios.weights @ np.abs(scenarios.next_changes @ target_vector))
            elif strategy == "delta":
                numerical_primary = float(sum(abs(v @ target_vector) for v in delta_vectors.values()))
            elif strategy == "greek":
                numerical_primary = float(sum(abs(v @ target_vector) for v in baseline_vectors))
            elif strategy == "min_variance":
                numerical_primary = float(np.sum(np.abs(target_vector[idx] - continuous.value)))
            if numerical_primary > float(primary_limit.value) + 1e-5 or (
                "next_selection_limit" in metrics
                and float(scenarios.weights @ np.abs(scenarios.next_changes @ target_vector))
                > metrics["next_selection_limit"] + 1e-5
            ):
                return self._unchanged(snapshot, scenarios, error, quotes, "OBJECTIVE_CERTIFICATE_FAILED")
            metrics["returned_primary_objective"] = numerical_primary
            # No-trade includes its own carry, rather than comparing net against gross.
            current_carry = cash_carry(
                m,
                float(snapshot.cash),
                sum(
                    max(0, -actual.get(i, 0)) * quotes[i].spot for i in ids if m.contract_map[i].kind == "STK"
                ),
                days,
                float(snapshot.nav),
            )
            old_pnl = error + scenarios.changes @ quantities + current_carry
            differences = np.abs(old_pnl) - np.abs(new_pnl)
            uncertainty = (
                float(np.std(differences, ddof=1) / np.sqrt(len(differences))) if len(differences) > 1 else 0
            )
            return Target(
                quantities=result,
                expected_score=float(scenarios.weights @ np.abs(new_pnl)),
                current_score=float(scenarios.weights @ np.abs(old_pnl)),
                uncertainty=uncertainty,
                status="OPTIMAL",
                snapshot_id=snapshot.version,
                scenario_id=scenarios.identifier,
                config_hash=m.config_hash,
                estimated_cost=cost,
                explanation=(
                    f"{strategy}: integer scenario hedge; {scenarios.source}. Costs are included once. "
                    + (
                        "Secondary ranking completed."
                        if metrics["ranking_complete"]
                        else f"Retained certified {accepted['stage']} target; secondary ranking budget exhausted."
                    )
                ),
            )
        except cp.error.SolverError as exc:
            return self._unchanged(snapshot, scenarios, error, quotes, f"SOLVER_ERROR:{exc}")

    def _unchanged(self, snapshot, scenarios, error, quotes, status):
        q = np.array([snapshot.quantities().get(i, 0) for i in scenarios.ids])
        borrow = sum(
            max(0, -amount) * quotes[i].spot
            for i, amount in snapshot.quantities().items()
            if i in quotes and self.manifest.contract_map[i].kind == "STK"
        )
        carry = cash_carry(
            self.manifest, float(snapshot.cash), borrow, scenarios.calendar_days, float(snapshot.nav)
        )
        score = float(scenarios.weights @ np.abs(error + scenarios.changes @ q + carry))
        return Target(
            quantities=snapshot.quantities(),
            expected_score=score,
            current_score=score,
            uncertainty=0,
            status=status,
            snapshot_id=snapshot.version,
            scenario_id=scenarios.identifier,
            config_hash=self.manifest.config_hash,
            explanation="No trade: optimizer did not return an accepted optimum.",
        )
