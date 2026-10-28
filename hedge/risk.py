from __future__ import annotations

from datetime import timedelta

import numpy as np

from .config import exposure_group
from .models import RiskResult


def gross_weight(manifest, contract, quote):
    """A discretionary position's share of its underlying notional in the gross cap: 1, or an
    option's |delta| under risk.gross_basis "delta"."""
    if manifest.risk.gross_basis == "delta" and contract.kind == "OPT":
        return abs(quote.delta or 0)
    return 1.0


class MandateRegistry:
    def __init__(self, manifest):
        self.manifest = manifest

    def check(self, quantities):
        return [
            f"Mandatory conId {i}: expected {q}, actual {quantities.get(i, 0)}"
            for i, q in self.manifest.mandatory.items()
            if quantities.get(i, 0) != q
        ]


class RiskEngine:
    def __init__(self, manifest):
        self.manifest = manifest
        self.baseline_nav = float(manifest.initial_nav)

    def evaluate(self, snapshot, quotes, scenarios, quantities, costs=0):
        c_map = self.manifest.contract_map
        budget = min(self.baseline_nav, float(snapshot.nav))
        reasons = []
        if budget <= 0:
            return RiskResult(passed=False, reasons=("Nonpositive NAV",), metrics={})
        unknown = set(quantities) - c_map.keys()
        if unknown:
            return RiskResult(
                passed=False, reasons=(f"Unsupported actual positions: {sorted(unknown)}",), metrics={}
            )
        active = {i: q for i, q in quantities.items() if q}
        if not set(active).issubset(quotes) or not set(active).issubset(scenarios.ids):
            return RiskResult(
                passed=False, reasons=("Missing quotes or stress coverage for holdings",), metrics={}
            )
        # Gross notional caps discretionary leverage. The mandated legs are fixed by the contest
        # (the SPY straddle alone is ~$3.85M of underlying) and are reported separately.
        gross, mandated_gross, delta, stock, names = 0.0, 0.0, {}, {}, {}
        gamma, vega, theta = {}, {}, {}
        rho, beta_delta, rho_missing = {}, 0.0, set()
        actual = snapshot.quantities()
        margin_increment, assignment_cash = 0.0, 0.0
        for i, quantity in active.items():
            c, q = c_map[i], quotes[i]
            if i in self.manifest.mandatory:
                mandated_gross += abs(quantity) * c.multiplier * q.spot
            else:
                gross += abs(quantity) * c.multiplier * q.spot * gross_weight(self.manifest, c, q)
            delta[c.symbol] = delta.get(c.symbol, 0) + quantity * c.multiplier * q.delta * q.spot
            gamma[c.symbol] = gamma.get(c.symbol, 0) + quantity * c.multiplier * q.gamma
            vega[c.symbol] = vega.get(c.symbol, 0) + quantity * c.multiplier * q.vega / 100
            theta[c.symbol] = theta.get(c.symbol, 0) + quantity * c.multiplier * q.theta
            modeled_rho = scenarios.rhos.get(i, q.rho)
            if c.kind == "OPT" and modeled_rho is None:
                rho_missing.add(c.symbol)
            else:
                rho[c.symbol] = rho.get(c.symbol, 0) + quantity * c.multiplier * (modeled_rho or 0) / 100
            if c.symbol in scenarios.betas:
                beta_delta += quantity * c.multiplier * q.delta * q.spot * scenarios.betas[c.symbol]
            if c.kind == "STK":
                stock[c.symbol] = stock.get(c.symbol, 0) + abs(quantity) * q.spot
                margin_increment += max(0, abs(quantity) - abs(actual.get(i, 0))) * q.spot * 0.5
            elif i not in self.manifest.mandatory or quantity < 0:
                if i not in self.manifest.mandatory:
                    names[c.symbol] = names.get(c.symbol, 0) + 1
                    if abs(quantity) > self.manifest.risk.contracts_per_option:
                        reasons.append(f"Option quantity cap: {c.label}")
                increment = max(0, abs(quantity) - abs(actual.get(i, 0)))
                margin_increment += increment * c.multiplier * (q.mid + 0.2 * q.spot)
                if quantity < 0:
                    assignment_cash += abs(quantity) * c.multiplier * c.strike
        values = np.array([quantities.get(i, 0) for i in scenarios.ids])
        position_stress = max(0.0, float(-np.min(scenarios.stress @ values)))
        stress_loss = max(0.0, float(-np.min(scenarios.stress @ values)) + costs)
        remaining_liquidity = float(snapshot.excess_liquidity) - margin_increment - costs
        metrics = {
            "budget": budget,
            "stress_loss": stress_loss,
            "position_stress_loss": position_stress,  # the holdings' alone, without trading costs
            "stress_limit": 0.03 * budget,
            "gross_notional": gross,
            "mandated_gross_notional": mandated_gross,
            "excess_liquidity_estimate": remaining_liquidity,
            "assignment_cash": assignment_cash,
            "estimated_added_margin": margin_increment,
        }
        beta_available = all(c_map[i].symbol in scenarios.betas for i in active)
        metrics["coverage:beta"] = float(beta_available)
        metrics["coverage:rho"] = float(not rho_missing)
        if beta_available:
            metrics["beta_delta_dollars:portfolio"] = beta_delta
            metrics["beta_nav:portfolio"] = beta_delta / budget
        forecast = np.abs(scenarios.changes @ values)
        cutoff = np.quantile(forecast, 0.95)
        metrics["absolute_pnl_cvar95"] = float(forecast[forecast >= cutoff].mean())
        if stress_loss > self.manifest.risk.stress_loss * budget + 0.01:
            reasons.append("Stress loss exceeds 3% budget")
        if gross > self.manifest.risk.gross_ceiling() * budget + 0.01:
            reasons.append("Gross underlying-equivalent notional exceeds cap")
        if remaining_liquidity < self.manifest.risk.liquidity_buffer * budget:
            reasons.append("Insufficient excess liquidity after conservative margin estimate")
        for symbol in sorted(delta):
            if symbol in scenarios.betas:
                metrics[f"beta:{symbol}"] = scenarios.betas[symbol]
            if symbol not in rho_missing:
                metrics[f"rho_per_point:{symbol}"] = rho.get(symbol, 0)
            metrics.update(
                {
                    f"delta:{symbol}": delta[symbol],
                    f"gamma:{symbol}": gamma[symbol],
                    f"vega_per_point:{symbol}": vega[symbol],
                    f"theta_per_day:{symbol}": theta[symbol],
                    f"stock_notional:{symbol}": stock.get(symbol, 0),
                    f"option_names:{symbol}": names.get(symbol, 0),
                }
            )
            if stock.get(symbol, 0) > self.manifest.risk.stock_notional * budget + 0.01:
                reasons.append(f"Stock notional cap: {symbol}")
            if names.get(symbol, 0) > self.manifest.risk.option_names:
                reasons.append(f"Option cardinality cap: {symbol}")
        # The residual-delta cap applies per exposure group: the S&P 500 funds (SPY, IVV, VOO,
        # SPLG) net, so an IVV short offsets SPY options' delta; any other symbol is its own group.
        residual = {}
        for symbol, value in delta.items():
            residual[exposure_group(symbol)] = residual.get(exposure_group(symbol), 0) + value
        # Market exposure for the risk-reducing gross exception: |beta-weighted delta| when every
        # symbol has a beta, else the residual deltas' total.
        metrics["market_exposure"] = (
            abs(beta_delta) if beta_available else sum(abs(v) for v in residual.values())
        )
        for group in sorted(residual):
            metrics[f"residual_delta:{group}"] = residual[group]
            if abs(residual[group]) > self.manifest.risk.residual_delta * budget + 0.01:
                reasons.append(f"Residual delta cap: {group}")
        return RiskResult(passed=not reasons, reasons=tuple(reasons), metrics=metrics)


class RiskGate:
    def __init__(self, manifest, demo=False):
        self.manifest, self.demo = manifest, demo
        self.registry = MandateRegistry(manifest)
        self.engine = RiskEngine(manifest)

    def _above_the_cap(self, snapshot, quotes, scenarios, actual, proposed):
        """The reason a trade into gross between gross_notional and gross_risk_reducing is refused,
        or None: it must lower both stress loss and market exposure against the holdings. Holding
        or shrinking gross there is always allowed."""
        limits = self.manifest.risk
        budget = proposed.metrics.get("budget", 0)
        gross = proposed.metrics.get("gross_notional", 0)
        if not limits.gross_risk_reducing or gross <= limits.gross_notional * budget + 0.01:
            return None
        current = self.engine.evaluate(snapshot, quotes, scenarios, actual)
        if gross <= current.metrics.get("gross_notional", 0) + 0.01:
            return None
        lower = all(
            proposed.metrics.get(k, float("inf")) <= current.metrics.get(k, 0) + 0.01
            for k in ("position_stress_loss", "market_exposure")
        )
        return (
            None
            if lower
            else f"Gross above {limits.gross_notional:g}x NAV only for trades that lower stress loss and "
            "market exposure"
        )

    def validate(
        self,
        snapshot,
        quotes,
        scenarios,
        quantities,
        now,
        costs=0,
        corrective=False,
        day_zero=False,
        require_improvement=True,
        post_market=False,
    ):
        reasons = []
        if snapshot.account != self.manifest.account:
            reasons.append("Wrong configured paper account")
        if snapshot.source != ("synthetic" if self.demo else "ibkr"):
            reasons.append("Broker source mismatch")
        if not snapshot.connected or not snapshot.reconciled:
            reasons.append("Broker disconnected or unreconciled")
        if now - snapshot.at > timedelta(seconds=10) or snapshot.at > now + timedelta(seconds=1):
            reasons.append("Stale/future portfolio snapshot")
        if now - snapshot.account_at > timedelta(seconds=self.manifest.risk.account_age):
            reasons.append("Stale account value")
        if snapshot.account_at > now + timedelta(seconds=1):
            reasons.append("Future account value")
        actual = snapshot.quantities()
        if not day_zero:
            reasons.extend(self.registry.check(actual))
            reasons.extend(self.registry.check(quantities))
        else:
            if (
                now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
                != self.manifest.purchase_session
            ):
                reasons.append("Mandatory setup only allowed on the configured purchase session")
            for i, required in self.manifest.mandatory.items():
                sign = 1 if required > 0 else -1
                if not 0 <= sign * actual.get(i, 0) <= sign * quantities.get(i, 0) <= abs(required):
                    reasons.append("Day 0 may only add missing mandatory quantity in its required direction")
        # Structural contest rules cannot be bypassed by corrective mode, an
        # unchanged forbidden holding, or a misconfigured discretionary allowlist.
        for i in set(actual) | set(quantities):
            c = self.manifest.contract_map.get(i)
            if c and (actual.get(i, 0) or quantities.get(i, 0)):
                if reason := self.manifest.prohibited_reason(c):
                    reasons.append(f"{reason}: conId {i}")
        for order in snapshot.orders:
            c = self.manifest.contract_map.get(order.con_id)
            if c and order.signed_remaining and (reason := self.manifest.prohibited_reason(c)):
                reasons.append(f"Forbidden working order: {reason}")
        ids = {i for i in quantities if quantities[i]} | {i for i in actual if actual[i]}
        for i in ids:
            c, q = self.manifest.contract_map.get(i), quotes.get(i)
            if c is None or q is None:
                reasons.append(f"Missing contract/quote: {i}")
                continue
            if (
                quantities.get(i, 0) != actual.get(i, 0)
                and i not in self.manifest.allowlist
                and not (day_zero and i in self.manifest.mandatory)
            ):
                # Existing out-of-screen holdings still need an explicit allowlist before trading.
                reasons.append(f"Instrument not approved: {i}")
            allowed_type = "synthetic" if self.demo else "live"
            traded = quantities.get(i, 0) != actual.get(i, 0)
            # Outside regular hours a held option is valued from its underlying, and a stock with a
            # crossed book at its midpoint ("model"): accepted for holding, never for trading.
            held_model = q.data_type == "model" and not traded
            if q.data_type != allowed_type and not held_model:
                reasons.append(f"Non-live quote: {i}")
            limits = self.manifest.risk
            # An unchanged holding only needs a current valuation; anything traded needs a fresh book.
            max_age = (
                (limits.option_quote_age if traded else limits.held_option_quote_age)
                if c.kind == "OPT"
                # After hours a quiet book values a held stock; a trade still needs a fresh one.
                else limits.held_option_quote_age
                if not traded and self.manifest.extended_session(now) is not None
                # Burner legs (post_market) trade on the burner's own quote age: Moomoo's after-hours
                # quotes ran 2-6 s old on Oct 27 and the 5 s limit refused 17 burner exits.
                else max(limits.stock_quote_age, self.manifest.burner.quote_age_seconds)
                if post_market
                else limits.stock_quote_age
            )
            greek_age = limits.greek_age if traded else max(limits.greek_age, limits.held_option_quote_age)
            if (now - q.observed_at).total_seconds() > max_age or q.observed_at > now + timedelta(seconds=1):
                reasons.append(f"Stale/future quote: {i}")
            if c.kind == "OPT" and (
                q.iv is None or q.greek_at is None or (now - q.greek_at).total_seconds() > greek_age
            ):
                reasons.append(f"Missing/stale option Greeks: {i}")
            if c.kind == "OPT" and q.greek_at is not None and q.greek_at > now + timedelta(seconds=1):
                reasons.append(f"Future option Greeks: {i}")
            if quantities.get(i, 0) != actual.get(i, 0):
                if c.kind == "STK" and quantities.get(i, 0) < min(0, actual.get(i, 0)):
                    if q.shortable_shares is None or abs(quantities[i]) > q.shortable_shares:
                        reasons.append(f"Stock borrow unavailable/insufficient: {i}")
                if not self.manifest.can_trade(now, c.kind, setup=day_zero, post_market=post_market):
                    reasons.append(f"Outside permitted order window: {i}")
                if quantities.get(i, 0) > actual.get(i, 0) and q.ask_size < 1:
                    reasons.append(f"No ask liquidity: {i}")
                if quantities.get(i, 0) < actual.get(i, 0) and q.bid_size < 1:
                    reasons.append(f"No bid liquidity: {i}")
        proposed = self.engine.evaluate(snapshot, quotes, scenarios, quantities, costs)
        if reason := self._above_the_cap(snapshot, quotes, scenarios, actual, proposed):
            reasons.append(reason)
        if not proposed.passed:
            if corrective:
                current = self.engine.evaluate(snapshot, quotes, scenarios, actual)
                # Conservative exception: structural violations never bypassed;
                # every continuous risk measure improves or stays unchanged.
                budget = current.metrics.get("budget", 0)
                bounds = {
                    "stress_loss": self.manifest.risk.stress_loss * budget,
                    "gross_notional": self.manifest.risk.gross_ceiling() * budget,
                }
                for key in current.metrics:
                    if key.startswith("residual_delta:"):
                        bounds[key] = self.manifest.risk.residual_delta * budget
                    elif key.startswith("stock_notional:"):
                        bounds[key] = self.manifest.risk.stock_notional * budget
                breached = {
                    k: abs(current.metrics[k])
                    for k, bound in bounds.items()
                    if abs(current.metrics[k]) > bound + 0.01
                }
                no_worse = all(
                    abs(proposed.metrics.get(k, float("inf"))) <= value + 0.01
                    for k, value in breached.items()
                )
                no_new = all(
                    abs(proposed.metrics.get(k, float("inf"))) <= bound + 0.01
                    for k, bound in bounds.items()
                    if k not in breached
                )
                improved = any(
                    abs(proposed.metrics.get(k, float("inf"))) < value - 0.01 for k, value in breached.items()
                )
                liquidity_bound = self.manifest.risk.liquidity_buffer * budget
                new_liquidity = proposed.metrics.get("excess_liquidity_estimate", 0)
                current_liquidity = current.metrics.get("excess_liquidity_estimate", 0)
                if current_liquidity < liquidity_bound:
                    no_worse &= new_liquidity >= current_liquidity
                    improved |= new_liquidity > current_liquidity + 0.01
                else:
                    no_new &= new_liquidity >= liquidity_bound
                structural = [
                    r for r in proposed.reasons if "quantity" in r or "cardinality" in r or "Missing" in r
                ]
                if not no_worse or not no_new or (require_improvement and not improved) or structural:
                    reasons.extend(proposed.reasons)
            else:
                reasons.extend(proposed.reasons)
        return RiskResult(passed=not reasons, reasons=tuple(dict.fromkeys(reasons)), metrics=proposed.metrics)
