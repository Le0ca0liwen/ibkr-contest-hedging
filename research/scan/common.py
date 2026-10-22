"""Scratch helpers: contest-shaped synthetic universe of N underlyings (read-only use of repo code)."""

from __future__ import annotations

import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

import os
# research/ root (env RESEARCH_ROOT) and the app source (env HEDGE_SRC, else REPO, else the repository root above
# research/; the study used commit 982eeed).
RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
sys.path.insert(0, os.environ.get("HEDGE_SRC") or os.environ.get("REPO") or str(RESEARCH_ROOT.parent))

from hedge.config import NY, Manifest  # noqa: E402
from hedge.models import Contract, PortfolioSnapshot, Position, Quote  # noqa: E402
from hedge.valuation import ValuationEngine  # noqa: E402

OUT = RESEARCH_ROOT / "scan" / "out"
OUT.mkdir(parents=True, exist_ok=True)
EXPIRY = date(2026, 11, 20)
DAY0 = date(2026, 10, 21)
SESSIONS = (
    date(2026, 10, 22), date(2026, 10, 23), date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 28),
    date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4),
)
# Mandated underlyings with Oct 20 quotes from the task.
MANDATED = [("SPCX", 147.24, 0.57), ("RKLB", 51.15, 0.79), ("SPY", 768.51, 0.136)]


def extra_names(count, seed=5):
    rng = np.random.default_rng(seed)
    names = []
    for k in range(count):
        spot = float(np.round(rng.uniform(20, 400), 2))
        iv = float(np.round(rng.uniform(0.18, 0.75), 3))
        names.append((f"X{k:03d}", spot, iv))
    return names


def step_for(symbol, spot):
    if symbol == "SPY":
        return 1.0
    if spot < 30:
        return 0.5
    if spot < 100:
        return 1.0
    return 5.0


def build_manifest(n_total, history_path, options_per_name=12, chain_strikes=None, policy=None):
    """n_total underlyings including SPCX/RKLB/SPY; each gets stock + `options_per_name` options."""
    names = MANDATED + extra_names(max(0, n_total - 3))
    names = names[:n_total] if n_total >= 3 else MANDATED
    next_id = 1000
    contracts, mandatory = [], {}
    stock_ids = {}
    for symbol, spot, iv in names:
        stock_ids[symbol] = next_id
        contracts.append(Contract(con_id=next_id, symbol=symbol, kind="STK"))
        next_id += 1
    legs = [
        ("SPCX", 130, "C", 25), ("SPCX", 145, "C", -50), ("SPCX", 160, "C", 25),
        ("RKLB", 45, "P", -25), ("RKLB", 55, "P", 25), ("SPY", 770, "C", 25), ("SPY", 770, "P", 25),
    ]
    mandated_keys = set()
    for symbol, strike, right, qty in legs:
        contracts.append(
            Contract(con_id=next_id, symbol=symbol, kind="OPT", multiplier=100, expiry=EXPIRY,
                     strike=strike, right=right, underlying_id=stock_ids[symbol])
        )
        mandatory[next_id] = qty
        mandated_keys.add((symbol, EXPIRY, float(strike), right))
        next_id += 1
    for symbol, spot, iv in names:
        if symbol == "SPCX":
            groups = [(EXPIRY, 8), (date(2026, 11, 27), 4)]
        elif symbol == "RKLB":
            groups = [(EXPIRY, 8), (date(2026, 11, 27), 4)]
        elif symbol == "SPY":
            groups = [(date(2026, 11, 19), 6), (date(2026, 11, 23), 6)]
        else:
            groups = [(EXPIRY, 6), (date(2026, 12, 18), 6)]
        if options_per_name != 12:  # full-chain variant: many more listed options than the screen keeps
            groups = [(e, options_per_name // len(groups)) for e, _ in groups]
        step = step_for(symbol, spot)
        center = round(spot / step) * step
        ladder = sorted({center + k * step for k in range(-40, 41) if center + k * step > 0},
                        key=lambda k: (abs(k / spot - 1), k))
        for expiry, count in groups:
            got = 0
            for strike in ladder:
                for right in ("C", "P"):
                    if got >= count:
                        break
                    if symbol == "RKLB" and expiry == EXPIRY and right == "C":
                        continue  # forbidden
                    if (symbol, expiry, float(strike), right) in mandated_keys:
                        continue
                    contracts.append(
                        Contract(con_id=next_id, symbol=symbol, kind="OPT", multiplier=100, expiry=expiry,
                                 strike=float(strike), right=right, underlying_id=stock_ids[symbol])
                    )
                    next_id += 1
                    got += 1
                if got >= count:
                    break
    data = dict(
        name="Scan scaling study",
        account="DU_SCANSTUDY",
        day_zero=DAY0,
        scoring_sessions=SESSIONS,
        valuation_convention="SYNTHETIC",
        report_source="SYNTHETIC scaling study",
        scoring_rule="calendar_interest_benchmark",
        scoring_annual_rate=Decimal("0.0344"),
        market_data_provider="moomoo",
        daily_stock_history=str(history_path),
        contest_rules="spcx_rklb_spy",
        contest_option_expiry=EXPIRY,
        contracts=tuple(contracts),
        mandatory=mandatory,
        universe="open",
        confirmed=True,
        policy={"cash_rate": 0.0344, "cash_rate_status": "modeled", "cash_rate_source": "contest", **(policy or {})},
    )
    return Manifest.model_validate(data), names


def true_model(names, seed=11):
    """Market + sector factor model. 'space' sector includes SPCX, RKLB and 1/6 of extras."""
    rng = np.random.default_rng(seed)
    params = {}
    for k, (symbol, spot, iv) in enumerate(names):
        daily = iv / np.sqrt(252)
        if symbol == "SPY":
            beta, sector, s_load = 1.0, None, 0.0
        else:
            beta = float(rng.uniform(0.6, 1.6))
            sector = "space" if symbol in ("SPCX", "RKLB") or k % 6 == 0 else f"sec{k % 5}"
            s_load = float(rng.uniform(0.4, 1.0))
        params[symbol] = (beta, sector, s_load, daily)
    return params


def true_covariance(names, params, market_vol=0.0086, sector_vol=0.012):
    symbols = sorted(s for s, _, _ in names)
    n = len(symbols)
    cov = np.zeros((n, n))
    for a, sa in enumerate(symbols):
        ba, seca, la, da = params[sa]
        for b, sb in enumerate(symbols):
            bb, secb, lb, db = params[sb]
            v = ba * bb * market_vol**2
            if seca is not None and seca == secb:
                v += la * lb * sector_vol**2
            cov[a, b] = v
        sys_var = cov[a, a]
        cov[a, a] = max(da**2, sys_var * 1.05)  # idiosyncratic fills to target daily vol
    return symbols, cov


def write_history(names, path, sessions_total=250, spcx_sessions=90, seed=3, end=date(2026, 10, 21)):
    """Joint daily closes through Oct 21 (completed before Oct 22); SPCX listed June 12 (90 sessions to Oct 20)."""
    cal = xcals.get_calendar("XNYS")
    days = cal.sessions_window(cal.date_to_session(pd.Timestamp(end), direction="previous"), -sessions_total)
    params = true_model(names)
    symbols, cov = true_covariance(names, params)
    rng = np.random.default_rng(seed)
    rets = rng.multivariate_normal(np.zeros(len(symbols)), cov, size=len(days))
    rows = []
    spots = {s: p for s, p, _ in names}
    for j, s in enumerate(symbols):
        closes = spots[s] * np.exp(np.cumsum(rets[:, j]) - np.sum(rets[:, j]))
        keep = slice(len(days) - spcx_sessions - 1, None) if s == "SPCX" else slice(None)
        for d, c in zip(days[keep], closes[keep], strict=True):
            rows.append({"date": str(d.date()), "symbol": s, "close": float(c)})
    pd.DataFrame(rows).to_csv(path, index=False)
    return symbols, cov


def make_quotes(m, names, now):
    val = ValuationEngine(m)
    info = {s: (p, iv) for s, p, iv in names}
    quotes = {}
    for c in m.contracts:
        spot, iv = info[c.symbol]
        if c.kind == "STK":
            quotes[c.con_id] = Quote(
                provider="moomoo", con_id=c.con_id, bid=round(spot - 0.01, 2), ask=round(spot + 0.01, 2),
                bid_size=500, ask_size=500, observed_at=now, data_type="live", spot=spot, iv=None,
                greek_at=now, shortable_shares=5000,
            )
            continue
        # light skew so IV differs by strike
        smile = iv * (1 + 0.15 * (c.strike / spot - 1) ** 2 - 0.1 * (c.strike / spot - 1))
        price = val.price(c, spot, smile, now)
        g = val.greeks(c, spot, smile, now)
        spread = float(np.clip(round(0.03 * price, 2), 0.05, 0.45))
        quotes[c.con_id] = Quote(
            provider="moomoo", con_id=c.con_id, bid=max(0.01, round(price - spread / 2, 2)),
            ask=round(price + spread / 2, 2), bid_size=20, ask_size=20, observed_at=now, data_type="live",
            spot=spot, iv=smile, greek_at=now, model_price=price, **g,
        )
    return quotes


def make_snapshot(m, quotes, now, gap=500.0):
    nav = float(m.initial_nav) + gap
    mark = sum(quotes[i].mid * q * m.contract_map[i].multiplier for i, q in m.mandatory.items())
    return PortfolioSnapshot(
        account=m.account, at=now, account_at=now, nav=Decimal(str(round(nav, 2))),
        cash=Decimal(str(round(nav - mark, 2))), excess_liquidity=Decimal(str(0.8 * nav)),
        initial_margin=Decimal(str(0.2 * nav)),
        positions=tuple(Position(con_id=i, quantity=q) for i, q in m.mandatory.items()),
    )


def now_at(hour=10, minute=30, day=SESSIONS[0]):
    return datetime.combine(day, datetime.min.time(), NY).replace(hour=hour, minute=minute)
