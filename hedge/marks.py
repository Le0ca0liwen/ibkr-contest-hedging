"""OCC-like option closing marks: closing NBBO midpoint, implied vol, a smoothed IV curve per class
and expiry, then the price at the smoothed vol (Options Clearing Corporation's daily method).

A tight quote on the curve barely moves; a wide quote off the curve is pulled toward it (Oct 22:
SPCX 130C 18.35/18.55 marked 18.4774, RKLB 55P 6.55/7.40 marked at its bid). Every mark stays
inside its closing bid/ask, the bounds the statement can land in. European Black-76 on the
put-call-parity forward, pure Python. Each option's curve is fitted over its WINDOW nearest strikes:
one quadratic is too stiff for a whole chain and would push tight, fair quotes to their bid or ask.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time

from .config import NY

YEAR = 365 * 86400
MIN_T = 3600 / YEAR  # one hour
MIN_WIDTH = 0.005  # IV spread floor for the fit weights
MIN_VOL = 0.005
HUBER = 0.5  # residuals beyond half the bid-ask IV width (the curve outside the book) weigh less
PASSES = 2
WINDOW = 7  # strikes per fit, nearest in log-strike to the option marked
RIGHTS = {"C": "C", "CALL": "C", "P": "P", "PUT": "P"}


def _cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def _right(value):
    return RIGHTS.get(str(value).strip().upper())


def bs_price(forward, strike, t, rate, vol, right):
    """European Black-76 price on the forward, discounted at exp(-rate * t); the discounted intrinsic
    value without vol, time or a positive forward and strike. `right` C/P (or CALL/PUT)."""
    side = _right(right)
    if side is None:
        raise ValueError(f"right must be C or P: {right!r}")
    discount = math.exp(-rate * t)
    if vol <= 0 or t <= 0 or forward <= 0 or strike <= 0:
        return discount * max(forward - strike if side == "C" else strike - forward, 0.0)
    sd = vol * math.sqrt(t)
    d1 = (math.log(forward / strike) + sd * sd / 2) / sd
    d2 = d1 - sd
    if side == "C":
        return discount * (forward * _cdf(d1) - strike * _cdf(d2))
    return discount * (strike * _cdf(-d2) - forward * _cdf(-d1))


def _vega(forward, strike, t, rate, vol):
    sd = vol * math.sqrt(t)
    d1 = (math.log(forward / strike) + sd * sd / 2) / sd
    return math.exp(-rate * t) * forward * math.sqrt(t) * math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)


def implied_vol(price, forward, strike, t, rate, right):
    """Black-76 implied vol by bisection; None outside the no-arbitrage bounds."""
    right = _right(right)
    if right is None or not all(math.isfinite(x) for x in (price, forward, strike, t, rate)):
        return None
    if forward <= 0 or strike <= 0 or t <= 0:
        return None
    discount = math.exp(-rate * t)
    lower = discount * max(forward - strike if right == "C" else strike - forward, 0.0)
    upper = discount * (forward if right == "C" else strike)
    if not lower < price < upper:
        return None
    lo, hi = 0.0, 1.0
    while bs_price(forward, strike, t, rate, hi, right) < price:
        hi *= 2
        if hi > 1000:
            return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(forward, strike, t, rate, mid, right) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    vol = (lo + hi) / 2
    return vol if vol > 0 else None


def _book(quote):
    try:
        bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask < bid:
        return None
    return bid, ask


def _strike(quote):
    try:
        strike = float(quote.get("strike"))
    except (TypeError, ValueError):
        return None
    return strike if math.isfinite(strike) and strike > 0 else None


def _expiry(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def implied_forward(quotes, spot, t, rate):
    """Median over strikes with a valid call and put book of K + exp(rate * t) * (C_mid - P_mid);
    spot * exp(rate * t) without any such pair (nan without a spot either)."""
    mids = {}
    for q in quotes:
        book, strike, right = _book(q), _strike(q), _right(q.get("right"))
        if book and strike and right:
            mids.setdefault(strike, {})[right] = sum(book) / 2
    growth = math.exp(rate * t)
    values = sorted(k + growth * (m["C"] - m["P"]) for k, m in mids.items() if "C" in m and "P" in m)
    values = [v for v in values if v > 0]
    if not values:
        try:
            return float(spot) * growth
        except (TypeError, ValueError):
            return math.nan
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def _solve(a, b):
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-300:
            return None
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in reversed(range(n)):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def _fit(points, degree):
    """Weighted least squares polynomial IV(k) through (k, iv, width) points with weights
    1 / width^2, then Huber reweighting passes (residuals in units of each point's IV width).
    Coefficients from the constant term up; None when singular."""
    base = [1 / max(width, MIN_WIDTH) ** 2 for _, _, width in points]
    weights, n = base, degree + 1
    for _ in range(PASSES + 1):
        a = [[0.0] * n for _ in range(n)]
        b = [0.0] * n
        for (k, iv, _), w in zip(points, weights, strict=True):
            for i in range(n):
                b[i] += w * k**i * iv
                for j in range(n):
                    a[i][j] += w * k ** (i + j)
        coefficients = _solve(a, b)
        if coefficients is None:
            return None
        weights = []
        for (k, iv, width), w in zip(points, base, strict=True):
            z = abs(iv - _poly(coefficients, k)) / max(width, MIN_WIDTH)
            weights.append(w if z <= HUBER else w * HUBER / z)
    return coefficients


def _poly(coefficients, k):
    return sum(c * k**p for p, c in enumerate(coefficients))


def _expiry_t(expiry, at):
    return max((datetime.combine(expiry, time(16), NY) - at).total_seconds() / YEAR, MIN_T)


def smooth_marks(chain, spot, at, rate):
    """{key: {"mark", "low", "high", "iv", "smoothed_iv", "source"}} for every valid book in `chain`
    (dicts with "key", "expiry" (date or ISO string), "strike", "right" (C/P), "bid", "ask") of one
    underlying at `spot`; `at` aware.

    Per expiry: the parity forward, IV at each midpoint, a weighted fit of IV against ln(K/F) over
    out-of-the-money quotes (in-the-money only at strikes without one) at the WINDOW strikes nearest
    the option (quadratic from 4 strikes, linear with 3), and the price at the fitted vol clipped to
    the bid/ask. Fewer than 3 strikes with an IV keep the midpoint. low/high are the bid/ask; invalid
    books, strikes, rights and expiries are left out."""
    groups = {}
    for q in chain:
        book, strike, right, expiry = _book(q), _strike(q), _right(q.get("right")), _expiry(q.get("expiry"))
        if book and strike and right and expiry:
            row = {"key": q["key"], "strike": strike, "right": right, "bid": book[0], "ask": book[1]}
            groups.setdefault(expiry, []).append(row)
    result = {}
    for expiry, rows in groups.items():
        t = _expiry_t(expiry, at)
        forward = implied_forward(rows, spot, t, rate)
        for row in rows:
            strike, right, bid, ask = row["strike"], row["right"], row["bid"], row["ask"]
            row["mid"] = (bid + ask) / 2
            row["iv"] = implied_vol(row["mid"], forward, strike, t, rate, right)
            row["otm"] = (right == "C") == (strike >= forward)
            if row["iv"] is not None:
                iv_bid = implied_vol(bid, forward, strike, t, rate, right)
                iv_ask = implied_vol(ask, forward, strike, t, rate, right)
                if iv_bid is None or iv_ask is None:  # a side outside the bounds: the spread over vega
                    vega = _vega(forward, strike, t, rate, row["iv"])
                    row["width"] = (ask - bid) / vega if vega > 0 else math.inf
                else:
                    row["width"] = iv_ask - iv_bid
        priced = [r for r in rows if r["iv"] is not None and math.isfinite(r["width"])]
        otm = {r["strike"] for r in priced if r["otm"]}
        fitted = [r for r in priced if r["otm"] or r["strike"] not in otm]
        strikes = sorted({r["strike"] for r in fitted})
        fits = {}
        for r in rows:
            window = tuple(sorted(sorted(strikes, key=lambda s: abs(math.log(s / r["strike"])))[:WINDOW]))
            if window not in fits:
                points = [
                    (math.log(f["strike"] / forward), f["iv"], f["width"])
                    for f in fitted
                    if f["strike"] in window
                ]
                fits[window] = _fit(points, 2 if len(window) >= 4 else 1) if len(window) >= 3 else None
            coefficients = fits[window]
            entry = {"mark": r["mid"], "low": r["bid"], "high": r["ask"], "iv": r["iv"], "smoothed_iv": None}
            if coefficients is None:
                entry["source"] = "closing midpoint"
            else:
                smoothed = max(_poly(coefficients, math.log(r["strike"] / forward)), MIN_VOL)
                price = bs_price(forward, r["strike"], t, rate, smoothed, r["right"])
                entry.update(
                    mark=min(max(price, r["bid"]), r["ask"]),
                    smoothed_iv=smoothed,
                    source=f"smoothed IV ({len(window)} strikes)",
                )
            result[r["key"]] = entry
    return result
