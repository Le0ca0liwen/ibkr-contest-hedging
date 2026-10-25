"""The rebuilt OCC end-of-day price: tight quotes on the curve stay near their mid, a wide quote off
the curve is pulled toward it inside its bid/ask, and filtered quotes come from the surface."""

import math
from datetime import date, datetime

import pytest

from hedge.config import NY
from hedge.occ_marks import _forward, _Tree, chain_marks, occ_marks, symbol_params

AT = datetime(2026, 10, 23, 16, tzinfo=NY)
EXPIRY = date(2026, 11, 20)
SPOT, RATE = 100.0, 0.034
DAYS = (EXPIRY - AT.date()).days


def true_prices(strikes=range(80, 121, 5)):
    tree = _Tree(SPOT, RATE, 0.0, DAYS, 201)
    prices = {}
    for k in strikes:
        vol = 0.5 + 0.4 * math.log(k / SPOT) ** 2 - 0.1 * math.log(k / SPOT)
        for right in "CP":
            prices[f"{right}{k}"] = (k, right, tree.price(k, right, vol))
    return prices


def chain(prices, half=0.05, overrides=None):
    rows = []
    for key, (k, right, p) in prices.items():
        bid, ask = (overrides or {}).get(key, (round(p - half, 2), round(p + half, 2)))
        rows.append({"key": key, "expiry": EXPIRY, "strike": k, "right": right, "bid": bid, "ask": ask})
    return rows


def test_tight_quotes_on_the_curve_come_back_near_their_true_value():
    prices = true_prices()
    marks = occ_marks(chain(prices), SPOT, AT, RATE)
    for key, (_, _, p) in prices.items():
        assert marks[key]["fitted"] and marks[key]["source"].startswith("OCC rebuild")
        assert marks[key]["mark"] == pytest.approx(p, abs=0.05)  # within the book, near the truth
        assert marks[key]["low"] <= marks[key]["mark"] <= marks[key]["high"]


def test_a_wide_quote_off_the_curve_is_pulled_toward_it_inside_its_book():
    """Oct 22: RKLB 55P long, quoted 6.55/7.40, was marked at its 6.55 bid."""
    prices = true_prices()
    true = prices["P105"][2]
    wide = (round(true - 0.05, 2), round(true + 0.80, 2))  # mid 0.375 above the curve
    marks = occ_marks(chain(prices, overrides={"P105": wide}), SPOT, AT, RATE)
    assert wide[0] <= marks["P105"]["mark"] < (wide[0] + wide[1]) / 2 - 0.2
    assert marks["P105"]["mark"] == pytest.approx(true, abs=0.1)


def test_a_tight_call_keeps_its_value_against_a_wide_put_off_parity():
    """BA: every put quoted wide, one off parity. Weights scaled per side made the tight call and
    the wide put split the gap (C110 at its ask); one scale for both leaves the call at its value."""
    prices = true_prices()
    wide = {}
    for key, (k, right, p) in prices.items():
        if right == "P":
            shift = 0.3 if k == 110 else 0.0
            wide[key] = (round(p + shift - 0.4, 2), round(p + shift + 0.4, 2))
    rows = chain(prices, overrides=wide)
    joint = occ_marks(rows, SPOT, AT, RATE)
    side = occ_marks(rows, SPOT, AT, RATE, {"weight_norm": "side"})
    assert joint["C110"]["mark"] == pytest.approx(prices["C110"][2], abs=0.005)
    assert joint["P110"]["mark"] == pytest.approx(prices["P110"][2], abs=0.01)
    assert side["C110"]["mark"] == side["C110"]["high"]


def test_the_forward_interpolates_parity_at_spot_and_takes_the_nearest_pair_outside():
    def pair(strike, forward):
        return {
            "strike": strike,
            "C": {"mid": max(forward - strike, 0) + 2},
            "P": {"mid": max(strike - forward, 0) + 2},
        }

    pairs = [pair(95, 100.2), pair(105, 100.6)]
    assert _forward(pairs, 101, 0.1, 0.0, "interp") == pytest.approx(100.44)
    assert _forward(pairs, 90, 0.1, 0.0, "interp") == pytest.approx(100.2)  # below the strikes
    assert _forward(pairs, 101, 0.1, 0.0, "atm") == pytest.approx(100.6)


def test_spy_style_options_keep_the_at_the_money_parity_on_american_midpoints():
    assert {k: symbol_params("SPY")[k] for k in ("forward", "forward_space")} == {
        "forward": "atm",
        "forward_space": "mid",
    }
    assert {k: symbol_params("RKLB")[k] for k in ("forward", "forward_space")} == {
        "forward": "interp",
        "forward_space": "eu",
    }


def test_calls_fall_and_stay_convex_across_strikes():
    marks = occ_marks(chain(true_prices()), SPOT, AT, RATE)
    calls = [marks[f"C{k}"]["mark"] for k in range(80, 121, 5)]
    steps = [b - a for a, b in zip(calls, calls[1:], strict=False)]
    assert all(s < 0 for s in steps) and all(b >= a - 0.02 for a, b in zip(steps, steps[1:], strict=False))


def test_a_one_sided_strike_is_priced_from_the_surface_and_too_few_strikes_keep_the_mid():
    prices = true_prices()
    rows = [r for r in chain(prices) if r["key"] != "P95"]  # C95 has no put: filtered, then repriced
    marks = occ_marks(rows, SPOT, AT, RATE)
    assert not marks["C95"]["fitted"] and "surface" in marks["C95"]["source"]
    assert marks["C95"]["mark"] == pytest.approx(prices["C95"][2], abs=0.1)
    few = [r for r in chain(prices) if r["strike"] in (95, 100)]
    assert all(m["source"] == "closing midpoint" for m in occ_marks(few, SPOT, AT, RATE).values())


def test_a_one_sided_strike_can_be_kept_inside_its_own_book():
    """OCC: final prices stay within the bid/ask. A strike without its counterpart is priced from the
    surface; with clip_surface its valid book bounds that price."""
    prices = true_prices()
    true = prices["C95"][2]
    off = (round(true + 0.20, 2), round(true + 0.30, 2))  # the surface value sits below this book
    rows = [r for r in chain(prices, overrides={"C95": off}) if r["key"] != "P95"]
    free = occ_marks(rows, SPOT, AT, RATE, {"clip_surface": False})["C95"]
    kept = occ_marks(rows, SPOT, AT, RATE)["C95"]
    assert free["mark"] < off[0] and kept["mark"] == off[0] and "surface" in kept["source"]


def test_bad_books_are_left_out():
    prices = true_prices()
    rows = chain(prices, overrides={"C100": (0.0, 0.0), "P100": (5.2, 5.1)})
    marks = occ_marks(rows, SPOT, AT, RATE)
    assert "C100" not in marks and "P100" not in marks and "C105" in marks


def chain_record(expiries=(EXPIRY,)):
    options = []
    for expiry in expiries:
        for key, (k, right, p) in true_prices().items():
            options.append(
                {
                    "code": f"US.XYZ{expiry:%y%m%d}{key}",
                    "underlying": "XYZ",
                    "expiry": str(expiry),
                    "strike": k,
                    "right": right,
                    "bid": round(p - 0.05, 2),
                    "ask": round(p + 0.05, 2),
                }
            )
    return {"at": AT.isoformat(), "underlyings": {"XYZ": {"spot": SPOT}}, "options": options}


def test_chain_marks_spreads_expiries_over_processes_with_the_same_result():
    chain = chain_record((EXPIRY, date(2026, 12, 18)))
    alone = chain_marks(chain, RATE)
    assert len(alone) == len(chain["options"]) and all(m["fitted"] for m in alone.values())
    assert chain_marks(chain, RATE, workers=2) == alone


async def test_the_app_rebuilds_a_chain_in_its_own_process(manifest, store):
    """QuantLib holds the interpreter lock: the service runs python -m hedge.occ_marks instead."""
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    chain = chain_record()
    marks = await service._rebuild_occ(chain)
    assert marks == chain_marks(chain, manifest.risk_free_rate)


def test_the_fast_tree_implied_vol_reprices_the_quote():
    """The fixed point (Black vol of the price less its early-exercise premium) replaced QuantLib's
    solver, which left up to 2.6 cents of repricing error and took 3x as long."""
    tree = _Tree(SPOT, RATE, 0.0, DAYS, 201)
    forward, t = SPOT * math.exp(RATE * DAYS / 365), DAYS / 365
    # (A deep in-the-money put worth its intrinsic value has no implied vol to find.)
    for k, right, vol in ((80, "P", 0.7), (100, "C", 0.5), (108, "P", 0.45), (90, "C", 0.6)):
        price = tree.price(k, right, vol)
        found = tree.implied(k, right, price, forward, t, RATE)
        assert found == pytest.approx(vol, abs=1e-5)
        assert tree.price(k, right, found) == pytest.approx(price, abs=1e-6)
