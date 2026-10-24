"""OCC-like option closing marks: midpoint IV, a smoothed IV curve per expiry, the price at the
smoothed vol clipped to the closing bid/ask."""

import math
from datetime import date, datetime

import pytest

from hedge.config import NY
from hedge.marks import WINDOW, bs_price, implied_forward, implied_vol, smooth_marks

EXPIRY = date(2026, 11, 20)
AT = datetime(2026, 10, 23, 15, 56, tzinfo=NY)
T = (datetime(2026, 11, 20, 16, tzinfo=NY) - AT).total_seconds() / (365 * 86400)
RATE = 0.04


def smile(k):
    return 0.40 - 0.15 * k + 0.6 * k * k


def skew(k):
    """A hyperbolic equity skew: curved at the money, linear in the wings."""
    return math.sqrt(0.04 + 0.3 * (-0.6 * k + math.sqrt(k * k + 0.05**2)))


def synthetic(spot=100.0, vol=smile, strikes=range(80, 125, 5)):
    """A chain from a known smile, quoted a cent outside the true price on each side."""
    forward = spot * math.exp(RATE * T)
    chain, true = [], {}
    for strike in strikes:
        for right in "CP":
            price = bs_price(forward, strike, T, RATE, vol(math.log(strike / forward)), right)
            if price < 0.10:
                continue
            key = f"{strike:g}{right}"
            true[key] = price
            bid = round(math.floor(price * 100) / 100 - 0.01, 2)
            ask = round(math.ceil(price * 100) / 100 + 0.01, 2)
            chain.append(
                {"key": key, "expiry": EXPIRY, "strike": strike, "right": right, "bid": bid, "ask": ask}
            )
    return chain, true, forward


def quote(key, strike, right, bid, ask, expiry=EXPIRY):
    return {"key": key, "expiry": expiry, "strike": strike, "right": right, "bid": bid, "ask": ask}


def spcx():
    """SPCX Nov 20 at 15:56 on Oct 23, spot ~147.17."""
    return [
        quote("130C", 130, "C", 19.60, 20.40),
        quote("145C", 145, "C", 9.85, 10.00),
        quote("160C", 160, "C", 4.00, 4.10),
        quote("145P", 145, "P", 7.30, 7.45),
    ]


def test_black_76_round_trip_and_bounds():
    for right in "CP":
        price = bs_price(105, 100, 0.25, RATE, 0.35, right)
        assert implied_vol(price, 105, 100, 0.25, RATE, right) == pytest.approx(0.35, abs=1e-8)
    discount = math.exp(-RATE * 0.25)
    call, put = bs_price(105, 100, 0.25, RATE, 0.35, "C"), bs_price(105, 100, 0.25, RATE, 0.35, "P")
    assert call - put == pytest.approx(discount * (105 - 100))
    assert implied_vol(discount * 5 - 0.01, 105, 100, 0.25, RATE, "C") is None  # below intrinsic
    assert implied_vol(discount * 105, 105, 100, 0.25, RATE, "C") is None  # at the forward
    assert implied_vol(0, 105, 110, 0.25, RATE, "C") is None
    assert bs_price(105, 100, 0.25, RATE, 0.35, "call") == call
    assert bs_price(105, 100, 0.25, RATE, 0.35, "PUT") == put
    assert bs_price(0, 100, 0.25, RATE, 0.35, "P") == pytest.approx(discount * 100)
    with pytest.raises(ValueError):
        bs_price(105, 100, 0.25, RATE, 0.35, "X")
    assert implied_vol(call, 105, 100, 0.25, RATE, "X") is None


def test_implied_forward_from_parity_with_spot_fallback():
    chain, _, forward = synthetic()
    assert implied_forward(chain, 100, T, RATE) == pytest.approx(forward, abs=0.005)
    calls = [q for q in chain if q["right"] == "C"]
    assert implied_forward(calls, 100, T, RATE) == pytest.approx(100 * math.exp(RATE * T))
    assert math.isnan(implied_forward(calls, None, T, RATE))


def test_tight_chain_on_a_quadratic_smile_round_trips_within_half_a_tick():
    chain, true, _ = synthetic()
    marks = smooth_marks(chain, 100, AT, RATE)
    assert set(marks) == set(true)
    for key, mark in marks.items():
        assert mark["mark"] == pytest.approx(true[key], abs=0.005)
        assert mark["low"] <= mark["mark"] <= mark["high"]
        assert mark["source"] == f"smoothed IV ({WINDOW} strikes)"
        strike = float(key[:-1])
        assert mark["smoothed_iv"] == pytest.approx(
            smile(math.log(strike / (100 * math.exp(RATE * T)))), abs=0.003
        )


def test_wide_chain_on_a_skew_keeps_fair_quotes_inside_their_books():
    """One quadratic over 70-130 cannot follow the skew and pushed most marks to their bid or ask."""
    chain, true, _ = synthetic(vol=skew, strikes=[70 + 2.5 * i for i in range(25)])
    marks = smooth_marks(chain, 100, AT, RATE)
    assert len(marks) == len(true) > 30
    for key, mark in marks.items():
        assert mark["low"] < mark["mark"] < mark["high"]
        assert mark["mark"] == pytest.approx(true[key], abs=0.015)


@pytest.mark.parametrize(
    ("key", "low", "high"),
    [("90P", -0.05, 0.75), ("100C", -0.60, 0.20), ("110C", 0.10, 0.90)],
)
def test_wide_off_curve_quote_is_pulled_toward_the_curve(key, low, high):
    chain, true, _ = synthetic()
    before = smooth_marks(chain, 100, AT, RATE)
    for q in chain:
        if q["key"] == key:
            q["bid"], q["ask"] = round(true[key] + low, 2), round(true[key] + high, 2)
            mid = (q["bid"] + q["ask"]) / 2
    after = smooth_marks(chain, 100, AT, RATE)
    mark = after[key]
    assert mark["low"] <= mark["mark"] <= mark["high"]
    assert abs(mark["mark"] - true[key]) < abs(mid - true[key]) / 4
    if low < 0 < high:  # the curve's price is inside the book: the mark lands on it
        assert mark["mark"] == pytest.approx(true[key], abs=0.01)
    else:
        assert mark["mark"] == mark["low"]
    assert max(abs(after[k]["mark"] - before[k]["mark"]) for k in before if k != key) < 0.002


def test_tight_off_curve_quote_barely_drags_the_others():
    chain, true, _ = synthetic()
    before = smooth_marks(chain, 100, AT, RATE)
    for q in chain:
        if q["key"] == "105C":
            q["bid"], q["ask"] = round(q["bid"] - 0.25, 2), round(q["ask"] - 0.25, 2)
    after = smooth_marks(chain, 100, AT, RATE)
    assert after["105C"]["mark"] == after["105C"]["high"]
    assert max(abs(after[k]["mark"] - before[k]["mark"]) for k in before if k != "105C") < 0.015


def test_spcx_oct_23_wide_itm_call_marks_below_its_midpoint():
    marks = smooth_marks(spcx(), 147.17, AT, RATE)
    assert marks["130C"]["iv"] == pytest.approx(0.559, abs=0.003)
    assert marks["145C"]["iv"] == pytest.approx(0.533, abs=0.003)
    assert marks["160C"]["iv"] == pytest.approx(0.527, abs=0.003)
    assert 19.75 < marks["130C"]["mark"] < 19.95
    assert marks["130C"]["smoothed_iv"] < marks["130C"]["iv"]
    for key, (bid, ask) in {"145C": (9.85, 10.00), "160C": (4.00, 4.10), "145P": (7.30, 7.45)}.items():
        assert bid <= marks[key]["mark"] <= ask
        assert marks[key]["mark"] == pytest.approx((bid + ask) / 2, abs=0.015)
    assert {m["source"] for m in marks.values()} == {"smoothed IV (3 strikes)"}


def test_rights_and_expiries_in_other_spellings_mark_the_same():
    marks = smooth_marks(spcx(), 147.17, AT, RATE)
    spelled = [dict(q, right={"C": "call", "P": "PUT"}[q["right"]], expiry="2026-11-20") for q in spcx()]
    assert smooth_marks(spelled, 147.17, AT, RATE) == marks
    assert implied_forward(spelled, 147.17, T, RATE) == implied_forward(spcx(), 147.17, T, RATE)


def test_unusable_quotes_are_skipped_and_no_forward_keeps_the_midpoints():
    chain = spcx() + [
        {"key": "no strike", "expiry": EXPIRY, "strike": None, "right": "C", "bid": 1, "ask": 2},
        quote("zero strike", 0, "C", 1, 2),
        quote("bad right", 150, "X", 1, 2),
        {"key": "no right", "expiry": EXPIRY, "strike": 150, "bid": 1, "ask": 2},
        quote("no expiry", 150, "C", 1, 2, expiry=None),
        quote("bad expiry", 150, "C", 1, 2, expiry="soon"),
    ]
    assert smooth_marks(chain, 147.17, AT, RATE) == smooth_marks(spcx(), 147.17, AT, RATE)
    assert smooth_marks(spcx(), None, AT, RATE)["130C"]["source"] == "smoothed IV (3 strikes)"
    calls = [q for q in spcx() if q["right"] == "C"]
    marks = smooth_marks(calls, None, AT, RATE)  # no spot and no put-call pair: no forward
    assert {k: m["mark"] for k, m in marks.items()} == pytest.approx(
        {"130C": 20.0, "145C": 9.925, "160C": 4.05}
    )
    assert all(m["source"] == "closing midpoint" and m["iv"] is None for m in marks.values())


def test_two_strikes_keep_the_closing_midpoints():
    chain = [quote(1, 45, "P", 1.88, 1.98), quote(2, 55, "P", 6.55, 7.40)]
    marks = smooth_marks(chain, 51.22, datetime(2026, 10, 22, 16, tzinfo=NY), RATE)
    assert marks[1]["mark"] == pytest.approx(1.93)
    assert marks[2]["mark"] == pytest.approx(6.975)
    assert (marks[2]["low"], marks[2]["high"]) == (6.55, 7.40)
    assert all(m["source"] == "closing midpoint" and m["smoothed_iv"] is None for m in marks.values())
    assert marks[1]["iv"] > 0


def test_bad_books_are_skipped_and_expiries_fit_separately():
    later = date(2026, 12, 18)
    chain, true, _ = synthetic()
    chain += [
        quote("zero bid", 100, "C", 0, 0.05),
        quote("crossed", 105, "C", 3.10, 3.00),
        quote("missing", 110, "C", None, 1.30),
        quote("dec 100C", 100, "C", 7.00, 7.20, later),
        quote("dec 110C", 110, "C", 3.20, 3.40, later),
    ]
    marks = smooth_marks(chain, 100, AT, RATE)
    assert set(marks) == set(true) | {"dec 100C", "dec 110C"}
    assert marks["dec 100C"]["source"] == "closing midpoint"
    assert marks["90P"]["source"] == f"smoothed IV ({WINDOW} strikes)"


def test_expired_chain_floors_time_at_an_hour():
    chain, _, _ = synthetic()
    marks = smooth_marks(chain, 100, datetime(2026, 11, 21, 12, tzinfo=NY), RATE)
    assert marks
    assert all(m["low"] <= m["mark"] <= m["high"] for m in marks.values())
