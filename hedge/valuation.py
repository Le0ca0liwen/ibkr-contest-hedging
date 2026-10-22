from __future__ import annotations

import math
import threading
from datetime import datetime, time, timedelta

import QuantLib as ql

from .config import NY, Manifest
from .models import Contract, Quote

_QL_LOCK = threading.Lock()  # QuantLib evaluation date is global process state.


class BelowModelFloor(ValueError):
    """Deep in the money: the midpoint is at/below the model's zero-vol value, so IV is unidentified."""

    def __init__(self, message, floor):
        super().__init__(message)
        self.floor = floor


class ValuationEngine:
    """American exercise and cash dividends with fractional calendar-time scaling.

    A deterministic time change maps the remaining interval to a 3650-day QL
    interval. Rates and volatility are scaled so r*T, q*T and sigma*sqrt(T)
    remain unchanged; dividend dates are mapped into that same interval. This
    avoids whole-day rounding when repricing from morning to today's close.
    """

    def __init__(self, manifest: Manifest):
        self.manifest = manifest

    def price(
        self, c: Contract, spot: float, iv: float, at: datetime, european=False, *, risk_free_rate=None
    ) -> float:
        if c.kind == "STK":
            return spot
        if spot <= 0 or iv <= 0 or at.tzinfo is None:
            raise ValueError("Positive spot/IV and timezone-aware valuation time required")
        annual_rate = self.manifest.risk_free_rate if risk_free_rate is None else risk_free_rate
        if not math.isfinite(annual_rate):
            raise ValueError("Finite risk-free rate required")
        expiration = datetime.combine(c.expiry, time(16), NY)
        remaining = (expiration - at.astimezone(NY)).total_seconds() / (365 * 86400)
        payoff = max(spot - c.strike, 0) if c.right == "C" else max(c.strike - spot, 0)
        if remaining <= 0:
            return payoff
        with _QL_LOCK:
            original = ql.Settings.instance().evaluationDate
            start = ql.Date(1, 1, 2020)
            end = start + 3650
            represented = ql.Actual365Fixed().yearFraction(start, end)
            ratio = remaining / represented
            try:
                ql.Settings.instance().evaluationDate = start
                day_count = ql.Actual365Fixed()
                rate = ql.YieldTermStructureHandle(ql.FlatForward(start, annual_rate * ratio, day_count))
                yield_rate = self.manifest.dividend_yields.get(c.symbol, 0)
                dividends = ql.YieldTermStructureHandle(ql.FlatForward(start, yield_rate * ratio, day_count))
                vol = ql.BlackVolTermStructureHandle(
                    ql.BlackConstantVol(start, ql.NullCalendar(), iv * math.sqrt(ratio), day_count)
                )
                process = ql.BlackScholesMertonProcess(
                    ql.QuoteHandle(ql.SimpleQuote(spot)), dividends, rate, vol
                )
                exercise = ql.EuropeanExercise(end) if european else ql.AmericanExercise(start, end)
                option = ql.VanillaOption(
                    ql.PlainVanillaPayoff(ql.Option.Call if c.right == "C" else ql.Option.Put, c.strike),
                    exercise,
                )
                schedule = ql.DividendSchedule()
                for dividend in self.manifest.dividends.get(c.symbol, ()):
                    ex_at = datetime.combine(dividend.ex_date, time(9, 30), NY)
                    seconds = (ex_at - at.astimezone(NY)).total_seconds()
                    if 0 < seconds < remaining * 365 * 86400:
                        offset = max(1, min(3649, round(seconds / (remaining * 365 * 86400) * 3650)))
                        schedule.append(ql.FixedDividend(dividend.amount, start + offset))
                if european and not schedule:
                    engine = ql.AnalyticEuropeanEngine(process)
                elif not schedule:
                    engine = ql.BjerksundStenslandApproximationEngine(process)
                else:
                    engine = ql.FdBlackScholesVanillaEngine(process, schedule, 80, 120)
                option.setPricingEngine(engine)
                return float(option.NPV())
            finally:
                ql.Settings.instance().evaluationDate = original

    def implied_volatility(self, c, spot, market_price, at):
        """Bounded deterministic inversion of the same American pricing model."""
        if not math.isfinite(market_price) or market_price <= 0:
            raise ValueError("Positive finite midpoint required")
        lo, hi = 0.001, 5.0
        low_price, high_price = self.price(c, spot, lo, at), self.price(c, spot, hi, at)
        if market_price <= low_price + 0.0001:
            raise BelowModelFloor("Midpoint at or below the model floor (deep in the money)", low_price)
        if market_price >= high_price:
            raise ValueError("Midpoint outside identifiable model IV bounds")
        for _ in range(45):
            mid = (lo + hi) / 2
            price = self.price(c, spot, mid, at)
            if abs(price - market_price) < 0.0001:
                return mid
            if price < market_price:
                lo = mid
            else:
                hi = mid
        raise ValueError("Midpoint IV calibration did not converge")

    def greeks(self, c, spot, iv, at):
        base = self.price(c, spot, iv, at)
        ds = max(0.01, spot * 0.001)
        up, down = self.price(c, spot + ds, iv, at), self.price(c, spot - ds, iv, at)
        from datetime import timedelta

        return {
            "delta": (up - down) / (2 * ds),
            "gamma": (up - 2 * base + down) / ds**2,
            "vega": (self.price(c, spot, iv + 0.001, at) - self.price(c, spot, max(0.001, iv - 0.001), at))
            / 0.002,
            "theta": self.price(c, spot, iv, at + timedelta(days=1)) - base,
            "rho": self.rho(c, spot, iv, at),
        }

    def rho(self, c, spot, iv, at):
        if c.kind == "STK":
            return 0.0
        rate, bump = self.manifest.risk_free_rate, 0.0001
        return (
            self.price(c, spot, iv, at, risk_free_rate=rate + bump)
            - self.price(c, spot, iv, at, risk_free_rate=rate - bump)
        ) / (2 * bump)

    def scenario_change(
        self,
        c: Contract,
        quote: Quote,
        now,
        end,
        return_shift,
        iv_shift=0,
        skew=0,
        rate_shift=0,
        current=None,
    ):
        if c.kind == "STK":
            return quote.spot * return_shift
        if quote.iv is None:
            raise ValueError(f"Missing implied volatility for {c.label}")
        if current is None:  # Callers repricing many scenarios pass the shared starting price.
            current = self.price(c, quote.spot, quote.iv, now)
        moneyness = c.strike / quote.spot - 1
        future = self.price(
            c,
            quote.spot * (1 + return_shift),
            max(0.01, quote.iv + iv_shift + skew * moneyness),
            end,
            risk_free_rate=self.manifest.risk_free_rate + rate_shift,
        )
        # Anchor to observed mark; forecast incremental modeled movement, not an
        # immediate fictitious gain/loss from replacing IBKR's mark with our price.
        return future - current


class NAVTracker:
    """Anchor broker NAV; attribute intervening fills at their real prices.

    Trading exchanges cash for assets: only mark-to-fill differences and fees
    affect NAV immediately. Reset on broker NAV observation, never merely on an
    inventory change (which would erase paid costs while broker NAV lags).

    With IBKR's real-time daily P&L (about every 1-2 s) the estimate is instead the last SETTLED
    NetLiquidation rolled forward by the P&L change since: a NetLiquidation push that follows a fill
    within PNL_SETTLE is briefly wrong (by up to ~$1,000 on Oct 22) and is never adopted, and
    IBKR's own marks, fills and fees all come through the P&L. After hours the P&L keeps options
    static, so their model revaluation is added. A base never crosses a session phase.
    """

    PNL_MAX_AGE = timedelta(seconds=5)  # older P&L: fall back to fill attribution
    PNL_SETTLE = timedelta(seconds=20)  # no fill this long before a NetLiquidation push, or since
    PNL_PROMPT = timedelta(seconds=30)  # the push must be paired with a P&L reading this soon
    PNL_RESET_FLOOR = 25.0  # a new P&L day starts within this of zero ...
    PNL_RESET_STEP = 200.0  # ... after a one-step drop at least this large

    def __init__(self, manifest=None):
        self.key = None
        self.marks = {}
        self.anchor_quantities = {}
        self.executions = {}
        self.fees = {}
        self.manifest = manifest
        self.pnl_anchor = None  # (settled NetLiquidation, daily P&L read with it)
        self.pnl_key = None
        self.pnl_phase = None
        self.pnl_last = None
        self.option_refs = {}  # after hours: held options' model mid at the base
        self.source = "fill attribution"
        self.last_check = None  # latest settled push vs the stream's prediction

    def _phase(self, now):
        """Regular session, contest pre/post-market session or closed: a stream base never crosses
        one (IBKR's P&L keeps options static outside regular hours)."""
        m = self.manifest
        if m is None:
            return None
        if extended := m.extended_session(now):
            return extended
        day = now.astimezone(NY).date()
        return (
            "regular" if m.is_session(day) and m.session_open(day) <= now < m.session_close(day) else "closed"
        )

    def _reset(self, pnl):
        """IBKR started a new P&L day: the value dropped to about zero in one step."""
        return abs(pnl) <= max(self.PNL_RESET_FLOOR, 0.05 * abs(self.pnl_last)) and (
            abs(self.pnl_last - pnl) >= self.PNL_RESET_STEP
        )

    def _after_hours_options(self, snapshot, quotes, contracts, refs):
        """After hours IBKR's P&L keeps held options static while NetLiquidation re-marks them from
        the underlying (observed Oct 22): add their model revaluation since the base."""
        total = 0.0
        for p in snapshot.positions:
            q, c = quotes.get(p.con_id), contracts.get(p.con_id)
            if p.quantity and c is not None and c.kind == "OPT" and q is not None and q.data_type == "model":
                total += p.quantity * c.multiplier * (q.mid - refs.setdefault(p.con_id, q.mid))
        return total

    def _pnl_estimate(self, snapshot, quotes, contracts):
        """NAV from the P&L stream, or None to fall back (no fresh stream / no settled base yet)."""
        pnl_at = snapshot.pnl_at
        phase = self._phase(snapshot.at)
        if (
            snapshot.pnl_daily is None
            or pnl_at is None
            or snapshot.at - pnl_at > self.PNL_MAX_AGE
            or (self.pnl_anchor is not None and phase != self.pnl_phase)
        ):
            # Stale or resubscribed stream, or a new session phase: wait for the next settled push.
            self.pnl_anchor = self.pnl_key = self.pnl_last = None
            self.option_refs = {}
            return None
        pnl, nav = float(snapshot.pnl_daily), float(snapshot.nav)
        if self.pnl_anchor is not None and self.pnl_last is not None and self._reset(pnl):
            base, pnl0 = self.pnl_anchor
            self.pnl_anchor = (base + (self.pnl_last - pnl0), pnl)
        key = (snapshot.nav, snapshot.account_at)
        # The P&L read when the push arrived; else the current one if the push is recent.
        if snapshot.account_pnl_daily is not None:
            at_push = float(snapshot.account_pnl_daily)
        elif snapshot.at - snapshot.account_at <= self.PNL_PROMPT:
            at_push = pnl
        else:
            at_push = None
        settled = at_push is not None and not any(
            e.at >= snapshot.account_at - self.PNL_SETTLE for e in self.executions.values()
        )
        if key != self.pnl_key and settled:
            if self.pnl_anchor is not None:
                base, pnl0 = self.pnl_anchor
                predicted = (
                    base
                    + (at_push - pnl0)
                    + self._after_hours_options(snapshot, quotes, contracts, dict(self.option_refs))
                )
                self.last_check = {
                    "at": snapshot.account_at.isoformat(),
                    "net_liquidation": nav,
                    "stream_estimate": predicted,
                    "difference": nav - predicted,
                }
            self.pnl_anchor, self.pnl_key, self.pnl_phase = (nav, at_push), key, phase
            self.option_refs = {}
        self.pnl_last = pnl
        if self.pnl_anchor is None:
            return None
        base, pnl0 = self.pnl_anchor
        return base + (pnl - pnl0) + self._after_hours_options(snapshot, quotes, contracts, self.option_refs)

    def execution(self, execution):
        self.executions[execution.exec_id] = execution

    def fee(self, exec_id, amount):
        self.fees[exec_id] = float(amount)

    def estimate(self, snapshot, quotes, contracts):
        streamed = self._pnl_estimate(snapshot, quotes, contracts)
        self.source = "IBKR P&L stream (1 s)" if streamed is not None else "fill attribution"
        if streamed is not None:
            return streamed
        key = (snapshot.nav, snapshot.account_at)
        if key != self.key:
            self.key = key
            self.anchor_quantities = snapshot.quantities()
            self.marks = {p.con_id: quotes[p.con_id].mid for p in snapshot.positions if p.con_id in quotes}
        roots = {e.correction_of.rsplit(".", 1)[0] for e in self.executions.values() if e.correction_of}
        selected = {}
        for e in self.executions.values():
            root, _, suffix = e.exec_id.rpartition(".")
            key_exec = root if root in roots else e.exec_id
            revision = int(suffix) if root in roots and suffix.isdigit() else 0
            if key_exec not in selected or revision > selected[key_exec][0]:
                selected[key_exec] = (revision, e)
        after = [e for _, e in selected.values() if e.at > snapshot.account_at]
        expected = dict(self.anchor_quantities)
        for e in after:
            expected[e.con_id] = expected.get(e.con_id, 0) + e.signed_quantity
        if {i: q for i, q in expected.items() if q} != snapshot.quantities():
            raise ValueError("NAV attribution waiting for matching executions or refreshed broker NAV")
        adjustment = sum(
            quantity * contracts[i].multiplier * (quotes[i].mid - self.marks[i])
            for i, quantity in self.anchor_quantities.items()
            if i in quotes and i in self.marks
        )
        for e in after:
            if e.con_id not in quotes:
                raise ValueError("Fill quote missing from NAV attribution")
            adjustment += (
                e.signed_quantity * contracts[e.con_id].multiplier * (quotes[e.con_id].mid - float(e.price))
            )
            fee = self.fees.get(e.exec_id)
            if fee is None and self.manifest:
                rate = (
                    self.manifest.policy.commission_option
                    if contracts[e.con_id].kind == "OPT"
                    else self.manifest.policy.commission_share
                )
                fee = max(self.manifest.policy.minimum_commission, abs(e.signed_quantity) * rate)
                if contracts[e.con_id].kind == "STK":  # IBKR Fixed: capped at 1% of trade value
                    fee = min(fee, 0.01 * abs(e.signed_quantity) * float(e.price))
            adjustment -= fee or 0
        return float(snapshot.nav) + adjustment
