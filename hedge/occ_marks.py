"""The Options Clearing Corporation's end-of-day option price, rebuilt: the price IBKR's statement
uses for every listed option (IBKR FAQ 1199).

OCC's published method (SR-OCC-2019-005, 84 FR 32816; SR-OCC-2020-016, 85 FR 85788; Cboe: "In all
cases, OCC marks closing ... prices based on the 4:00 p.m. ET NBBO"), per underlying and expiry:
1. filter out quotes with too little time left, one-sided strikes (a call without a put or the
   reverse), zero asks, crossed books and American asks below intrinsic value;
2. estimate the forward (and with it the borrow) from near-the-money put-call parity;
3. "de-Americanize" the quotes (Leisen-Reimer tree), then find the prices closest to the targets
   (vega-weighted least squares) that stay inside each bid/ask, are monotone and convex in strike
   and satisfy put-call parity, and "re-Americanize" them;
4. price the filtered-out contracts from the fitted surface (linear in total variance).
The parameters OCC keeps confidential (weights, targets, filter thresholds) are PARAMS, tuned on
statement prices with ops/mark_backtest.py.
"""

from __future__ import annotations

import argparse
import json
import math
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, time
from multiprocessing import get_context
from pathlib import Path

import cvxpy as cp
import numpy as np
import QuantLib as ql

from .config import LATE_OPTION_SYMBOLS, NY
from .marks import _right, bs_price, implied_vol
from .valuation import _QL_LOCK

PARAMS = {
    # inv_spread2: a tight quote stays at its midpoint, a wide one moves toward the chain. Oct 23
    # (the one day with a full closing chain): $227 of option-value error on the six SPCX/RKLB legs,
    # against $526 for the midpoint, $367 for hedge/marks.py and $1,458 with inv_vega2.
    "weight": "inv_spread2",  # inv_spread2, inv_spread, inv_vega2 (squared IV-space errors), inv_vega, unit
    # One scale for a strike's call and put weights ("joint"), so a tight quote outweighs a wide one
    # on the other side and keeps its midpoint; "side" (each side scaled by its own tightest quote)
    # let a tight call and a wide put split a parity gap almost evenly (BA: $0.039 a contract
    # against $0.013 joint, RKLB $0.077 against $0.064).
    "weight_norm": "joint",
    # The forward: "fit" (the least-squares fit chooses it), "median5"/"atm" (parity of the five
    # nearest pairs / the nearest pair, fixed), "interp" (the parity forwards of the two pairs
    # around spot, interpolated at spot), "spot" (spot carried at the rate, fixed). IBKR's 16:00
    # books against the Oct 7-23 statements and Oct 22's 2,912 OCC prices (1,003 contracts with
    # both): single stocks follow their at-the-money parity (RKLB Nov 20: every strike's OCC pair
    # implies 51.58, the 50-strike midpoints 51.63, the five-strike median 51.40). Mean |error|
    # $0.048 a contract with these settings, $0.063 with the nearest pair and spot carry for SPY,
    # $0.115 for the midpoint (docs/STATEMENT_NAV.md, Oct 25).
    "forward": "interp",
    # SPY-style ETF options (config.LATE_OPTION_SYMBOLS): the at-the-money pair's parity on its
    # American midpoints, kept through the de-Americanizing ("forward_space_late": "mid"). It
    # follows the chain's dividends, where spot carried at the rate missed Dec 18 by $0.69. OCC's
    # SPY forward still moves by a whole-day shift the 16:00 books do not show (+0.25, -0.15 to
    # -0.22, -0.27 to -0.34 on Oct 21-23), so the fit stays clipped to the bid/ask.
    "forward_late": "atm",
    "forward_space": "eu",  # "eu": re-derive the forward from the de-Americanized midpoints; "mid": keep it
    "forward_space_late": "mid",
    "rate": 0.039,  # the forward's carry (SOFR-like), not the contest's 3.44% scoring rate
    "min_days": 1,  # quotes expiring sooner are filtered out
    "steps": 201,  # Leisen-Reimer tree steps
    "bound_penalty": 1e4,  # a crossed parity set may not fit every book: the overshoot costs this much
    "round": 4,  # decimals the statement prints
}
YEAR = 365.0  # calendar-day convention (SR-OCC-2024-016)


def _expiry(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value[:10]).date()
    return value.date() if isinstance(value, datetime) else value


def _years(expiry, at):
    close = datetime.combine(expiry, time(16), NY)
    return max((close - at).total_seconds() / 86400, 1 / 24) / YEAR


class _Tree:
    """American prices on a Leisen-Reimer tree with a flat rate and a borrow/dividend yield."""

    def __init__(self, spot, rate, borrow, days, steps):
        today = ql.Date(1, 1, 2030)
        counter = ql.Actual365Fixed()
        self.maturity = today + max(int(days), 1)
        self.today = today
        self.spot = ql.SimpleQuote(spot)
        self.vol = ql.SimpleQuote(0.3)
        self.process = ql.BlackScholesMertonProcess(
            ql.QuoteHandle(self.spot),
            ql.YieldTermStructureHandle(ql.FlatForward(today, borrow, counter)),
            ql.YieldTermStructureHandle(ql.FlatForward(today, rate, counter)),
            ql.BlackVolTermStructureHandle(
                ql.BlackConstantVol(today, ql.NullCalendar(), ql.QuoteHandle(self.vol), counter)
            ),
        )
        self.engine = ql.BinomialVanillaEngine(self.process, "lr", steps)

    def option(self, strike, right):
        kind = ql.Option.Call if right == "C" else ql.Option.Put
        option = ql.VanillaOption(
            ql.PlainVanillaPayoff(kind, strike), ql.AmericanExercise(self.today, self.maturity)
        )
        option.setPricingEngine(self.engine)
        return option

    def _run(self, work):
        with _QL_LOCK:  # QuantLib's evaluation date is process-wide (hedge/valuation.py)
            original = ql.Settings.instance().evaluationDate
            ql.Settings.instance().evaluationDate = self.today
            try:
                return work()
            finally:
                ql.Settings.instance().evaluationDate = original

    def price(self, strike, right, vol):
        def work():
            self.vol.setValue(max(vol, 1e-4))
            return self.option(strike, right).NPV()

        return self._run(work)

    def implied(self, strike, right, price, forward=None, t=None, rate=None):
        """The vol at which the tree prices `price`. Given the forward, by the fixed point
        vol = Black vol of (price - early-exercise premium at vol): two or three tree prices, where
        QuantLib's solver takes about thirteen; that solver when the iteration does not settle."""
        if forward is not None:
            vol = implied_vol(price, forward, strike, t, rate, right)
            for _ in range(5):
                if vol is None:
                    break
                premium = self.price(strike, right, vol) - bs_price(forward, strike, t, rate, vol, right)
                following = implied_vol(max(price - premium, 1e-9), forward, strike, t, rate, right)
                if following is not None and abs(following - vol) < 1e-6:
                    return following
                vol = following

        def work():
            try:
                return self.option(strike, right).impliedVolatility(price, self.process, 1e-7, 500, 1e-4, 5.0)
            except RuntimeError:
                return None

        return self._run(work)


def _forward(pairs, spot, t, rate, mode="fit", key="mid"):
    """The expiry's forward: spot carried at `rate` ("spot"), the parity forwards of the two pairs
    around spot interpolated at spot ("interp"; the nearest pair's outside the strikes), the parity
    forward of the pair nearest the money ("atm"), else the median parity forward of the five
    nearest (`key`: the quotes' midpoints, or their de-Americanized ones). `pairs` are by strike."""
    if mode == "spot" or not pairs:
        return spot * math.exp(rate * t)
    if mode == "interp":
        strikes = [p["strike"] for p in pairs]
        if strikes[0] < spot < strikes[-1]:
            values = [p["strike"] + math.exp(rate * t) * (p["C"][key] - p["P"][key]) for p in pairs]
            return float(np.interp(spot, strikes, values))
        mode = "atm"
    near = sorted(pairs, key=lambda p: abs(p["strike"] - spot))[: 1 if mode == "atm" else 5]
    values = [p["strike"] + math.exp(rate * t) * (p["C"][key] - p["P"][key]) for p in near]
    return float(np.median(values))


def _weights(rows, kind, forward, t, rate):
    weights = []
    for row in rows:
        vol = implied_vol(row["eu_mid"], forward, row["strike"], t, rate, row["right"]) or 0.5
        sd = vol * math.sqrt(t)
        d1 = (math.log(forward / row["strike"]) + sd * sd / 2) / sd
        vega = max(
            math.exp(-rate * t) * forward * math.sqrt(t) * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi),
            1e-4,
        )
        spread = max(row["ask"] - row["bid"], 0.01)
        weights.append(
            {
                "inv_vega2": 1 / vega**2,
                "inv_vega": 1 / vega,
                "unit": 1.0,
                "inv_spread2": 1 / spread**2,
                "inv_spread": 1 / spread,
            }[kind]
        )
    return np.array(weights)


def _fit(pairs, forward, t, rate, params):
    """Smoothed European call and put prices per strike and the fitted forward (one quadratic program)."""
    strikes = np.array([p["strike"] for p in pairs])
    df = math.exp(-rate * t)
    calls = cp.Variable(len(pairs))
    fwd = cp.Variable()
    puts = calls - df * fwd + df * strikes  # put-call parity
    rows_c, rows_p = [p["C"] for p in pairs], [p["P"] for p in pairs]
    wc = _weights(rows_c, params["weight"], forward, t, rate)
    wp = _weights(rows_p, params["weight"], forward, t, rate)
    if params["weight_norm"] == "joint":
        scale = max(wc.max(), wp.max())
        wc, wp = wc / scale, wp / scale
    else:
        wc, wp = wc / wc.max(), wp / wp.max()
    mid_c = np.array([r["eu_mid"] for r in rows_c])
    mid_p = np.array([r["eu_mid"] for r in rows_p])
    low_c, high_c = np.array([r["eu_bid"] for r in rows_c]), np.array([r["eu_ask"] for r in rows_c])
    low_p, high_p = np.array([r["eu_bid"] for r in rows_p]), np.array([r["eu_ask"] for r in rows_p])
    over = cp.sum(
        cp.pos(low_c - calls) + cp.pos(calls - high_c) + cp.pos(low_p - puts) + cp.pos(puts - high_p)
    )
    objective = cp.sum(cp.multiply(wc, cp.square(calls - mid_c))) + cp.sum(
        cp.multiply(wp, cp.square(puts - mid_p))
    )
    constraints = [calls >= 0, puts >= 0, fwd >= 0.5 * forward, fwd <= 1.5 * forward]
    if params["forward"] != "fit":  # the forward is given, not fitted
        constraints.append(fwd == forward)
    if len(pairs) > 1:
        slopes = cp.multiply(cp.diff(calls), 1 / np.diff(strikes))
        constraints += [slopes <= 0, slopes >= -df]  # calls fall in strike, never faster than the bond
        if len(pairs) > 2:
            constraints.append(cp.diff(slopes) >= 0)  # convex
    problem = cp.Problem(cp.Minimize(objective + params["bound_penalty"] * over), constraints)
    problem.solve(solver=cp.CLARABEL)
    if calls.value is None:
        raise ValueError(f"OCC smoothing did not solve ({problem.status})")
    return calls.value, puts.value, float(fwd.value)


def occ_marks(chain, spot, at, rate, params=None):
    """{key: {"mark", "low", "high", "fitted", "source"}} for the valid books of one underlying's
    `chain` (dicts with "key", "expiry", "strike", "right", "bid", "ask") at its 16:00 `spot`; `at`
    is the aware snapshot time. Fitted contracts stay inside their bid/ask; filtered ones are priced
    from the fitted surface; an expiry without a fit keeps its midpoints."""
    params = {**PARAMS, **(params or {})}
    groups = {}
    for q in chain:
        right, expiry = _right(q.get("right")), _expiry(q.get("expiry"))
        try:
            strike, bid, ask = float(q["strike"]), float(q.get("bid") or 0), float(q.get("ask") or 0)
        except (TypeError, ValueError):
            continue
        if right and expiry and strike > 0 and ask > 0 and bid >= 0 and ask >= bid:
            row = {
                "key": q["key"],
                "strike": strike,
                "right": right,
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2,
            }
            groups.setdefault(expiry, []).append(row)
    result = {}
    for expiry, rows in groups.items():
        t = _years(expiry, at)
        days = (expiry - at.astimezone(NY).date()).days
        intrinsic = {"C": lambda k: max(spot - k, 0.0), "P": lambda k: max(k - spot, 0.0)}
        usable = [
            r for r in rows if days >= params["min_days"] and r["ask"] >= intrinsic[r["right"]](r["strike"])
        ]
        by_strike = {}
        for r in usable:
            by_strike.setdefault(r["strike"], {})[r["right"]] = r
        pairs = [{"strike": k, **v} for k, v in sorted(by_strike.items()) if "C" in v and "P" in v]
        fitted = {}
        if len(pairs) >= 3:
            try:
                fitted = _smooth(pairs, spot, t, rate, days, params)
            except (ValueError, cp.error.SolverError):
                fitted = {}
        for r in rows:
            low, high = r["bid"], r["ask"]
            if r["key"] in fitted:
                mark = min(max(fitted[r["key"]], low), high)
                result[r["key"]] = {
                    "mark": round(mark, params["round"]),
                    "low": low,
                    "high": high,
                    "fitted": True,
                    "source": f"OCC rebuild ({len(pairs)} strikes)",
                }
            elif fitted and "surface" in fitted:
                price = fitted["surface"](r["strike"], r["right"])
                result[r["key"]] = {
                    "mark": round(price, params["round"]),
                    "low": low,
                    "high": high,
                    "fitted": False,
                    "source": "OCC rebuild surface (filtered quote)",
                }
            else:
                result[r["key"]] = {
                    "mark": r["mid"],
                    "low": low,
                    "high": high,
                    "fitted": False,
                    "source": "closing midpoint",
                }
    return result


def _smooth(pairs, spot, t, rate, days, params):
    """{key: American smoothed price, "surface": f(strike, right)} for one expiry."""
    forward = _forward(pairs, spot, t, rate, params["forward"])
    for _ in range(2):  # the borrow depends on the forward and the forward on the de-Americanized quotes
        borrow = rate - math.log(forward / spot) / t
        tree = _Tree(spot, rate, borrow, days, params["steps"])
        for pair in pairs:
            for right in ("C", "P"):
                row = pair[right]
                vol = tree.implied(row["strike"], right, row["mid"], forward, t, rate)
                premium = 0.0
                if vol is not None:
                    premium = max(row["mid"] - bs_price(forward, row["strike"], t, rate, vol, right), 0.0)
                row["eep"] = premium
                row["eu_mid"] = max(row["mid"] - premium, 1e-6)
                row["eu_bid"], row["eu_ask"] = max(row["bid"] - premium, 0.0), max(row["ask"] - premium, 1e-6)
        key = "mid" if params["forward_space"] == "mid" else "eu_mid"
        forward = _forward(pairs, spot, t, rate, params["forward"], key)
    calls, puts, fitted_forward = _fit(pairs, forward, t, rate, params)
    borrow = rate - math.log(fitted_forward / spot) / t
    tree = _Tree(spot, rate, borrow, days, params["steps"])
    out, smile = {}, []
    for pair, call, put in zip(pairs, calls, puts, strict=True):
        for right, european in (("C", call), ("P", put)):
            row = pair[right]
            vol = implied_vol(max(european, 1e-6), fitted_forward, pair["strike"], t, rate, right)
            american = tree.price(pair["strike"], right, vol) if vol else european + row["eep"]
            out[row["key"]] = american
            if vol and (right == "C") == (pair["strike"] >= fitted_forward):
                smile.append((math.log(pair["strike"] / fitted_forward), vol * vol * t))

    def surface(strike, right):
        points = sorted(smile)
        k = math.log(strike / fitted_forward)
        xs, ws = [p[0] for p in points], [p[1] for p in points]
        variance = float(np.interp(k, xs, ws))  # flat beyond the fitted strikes
        return tree.price(strike, right, math.sqrt(max(variance, 1e-8) / t))

    out["surface"] = surface
    return out


def chain_marks(chain, rate, params=None, workers=1):
    """{Moomoo code: rebuilt price} for an option_chain record (ops/fetch_moomoo_chain.py): every
    contract the fit priced, from its own book or from the fitted surface. Each underlying's expiry
    is an independent fit, so workers > 1 spreads them over processes."""
    if not chain or not chain.get("options"):
        return {}
    at = datetime.fromisoformat(chain["at"])
    tasks = []
    for symbol, info in (chain.get("underlyings") or {}).items():
        spot = (info or {}).get("spot")
        expiries = {}
        for o in chain["options"]:
            if o.get("underlying") == symbol and o.get("bid") is not None and o.get("ask") is not None:
                expiries.setdefault(str(o.get("expiry")), []).append({**o, "key": o["code"]})
        chosen = symbol_params(symbol, params)
        if spot:
            tasks += [
                (rows, float(spot), at, chosen.get("rate") or rate, chosen) for rows in expiries.values()
            ]
    if workers > 1 and len(tasks) > 1:
        # spawn (macOS's default) everywhere: forking a process with threads can deadlock.
        with ProcessPoolExecutor(min(workers, len(tasks)), mp_context=get_context("spawn")) as pool:
            parts = list(pool.map(_chain_task, tasks))
    else:
        parts = [_chain_task(task) for task in tasks]
    return {
        code: mark
        for marks in parts
        for code, mark in marks.items()
        if mark["source"].startswith("OCC rebuild")
    }


def symbol_params(symbol, params=None):
    """PARAMS with `params` over them, and the ETF forward rules for SPY-style options."""
    chosen = {**PARAMS, **(params or {})}
    if symbol in LATE_OPTION_SYMBOLS:
        chosen["forward"] = chosen["forward_late"]
        chosen["forward_space"] = chosen["forward_space_late"]
    return chosen


def _chain_task(task):
    return occ_marks(*task)


def main(argv=None):
    """python -m hedge.occ_marks CHAIN.json OUTPUT.json --rate R [--workers N]. The app runs the
    rebuild in its own processes: QuantLib holds the interpreter lock for seconds at a time."""
    parser = argparse.ArgumentParser(description="Rebuild OCC's end-of-day option prices for a chain")
    parser.add_argument("chain")
    parser.add_argument("output")
    parser.add_argument("--rate", type=float, required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args(argv)
    marks = chain_marks(json.loads(Path(args.chain).read_text()), args.rate, workers=args.workers)
    output = Path(args.output)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(marks))
    temporary.replace(output)
    print(f"{len(marks)} OCC prices rebuilt")


if __name__ == "__main__":
    main()
