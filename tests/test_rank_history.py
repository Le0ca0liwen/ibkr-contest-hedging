from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from ops.rank_history import build, check, implied_previous, rank_day, wanted

SDK = SimpleNamespace(
    RET_OK=0,
    OptionMarket=SimpleNamespace(US_SECURITY="US"),
    OptionRankType=SimpleNamespace(VOLUME="VOLUME"),
    OptionRankIndicatorType=SimpleNamespace(OWNER_LIST="OWNER", OPTION_TYPE="TYPE", IN_THE_MONEY="ITM"),
)


def make_filter(kind, value_list=None, security_list=None):
    return (kind, tuple(value_list or security_list))


def row(code, price, change, volume, day="2026-10-22"):
    return {
        "code": code,
        "option_price": price,
        "change_ratio": change,
        "volume": volume,
        "bid_price": price - 0.05,
        "ask_price": price + 0.05,
        "trading_date": day,
    }


class FakeOpenD:
    """Ranks by part: two pages for calls out of the money, one for every other part."""

    def __init__(self, answer_day="2026-10-22"):
        self.calls, self.answer_day = [], answer_day

    def get_option_rank(self, market, sort, count, trading_date, page, filter_list):
        self.calls.append((trading_date, page, filter_list[1][1], filter_list[2][1]))
        right, money = filter_list[1][1][0], filter_list[2][1][0]
        if (right, money) == (1, 0) and page is None:
            return (
                0,
                pd.DataFrame([row("US.SPY261120C770000", 10.79, -30.461, 3858, self.answer_day)]),
                "p2",
                2,
            )
        if (right, money) == (1, 0):
            return 0, pd.DataFrame([row("US.SPY261120C800000", 1.0, 0.0, "N/A", self.answer_day)]), None, 2
        if (right, money) == (2, 1):
            return (
                0,
                pd.DataFrame([row("US.SPY261120P770000", 14.23, 50.744, 3042, self.answer_day)]),
                None,
                1,
            )
        return 0, pd.DataFrame([]), None, 0


def test_the_last_trade_and_its_change_give_the_day_befores_occ_price():
    """Oct 22's ranking: SPY Nov 20 770C last 10.79, -30.461%: OCC's Oct 21 price was 15.5166."""
    assert implied_previous(row("x", 10.79, -30.461, 1)) == pytest.approx(15.5166, abs=2e-4)
    assert implied_previous(row("x", 14.23, 50.744, 1)) == pytest.approx(9.4398, abs=2e-4)
    assert implied_previous(row("x", 0, 0, 1)) is None and implied_previous(row("x", 1.0, -100, 1)) is None


def test_each_part_is_paged_and_traded_contracts_give_the_previous_days_truth():
    opend = FakeOpenD()
    rows, errors = rank_day(opend, SDK, make_filter, "SPY", date(2026, 10, 22), sleep=lambda s: None)
    assert errors == [] and len(opend.calls) == 5  # four parts, the first one paged twice
    assert {c[2:] for c in opend.calls} == {((1,), (0,)), ((1,), (1,)), ((2,), (0,)), ((2,), (1,))}
    days = build({date(2026, 10, 22): rows}, checked_days={date(2026, 10, 22)})
    chain = days["2026-10-22"]["option_chain"][0]
    assert {o["code"] for o in chain["options"]} == {"US.SPY261120C770000", "US.SPY261120P770000"}
    assert [o["code"] for o in chain["untraded"]] == ["US.SPY261120C800000"]
    truth = {"2026-10-21": {"US.SPY261120C770000": 15.5166, "US.SPY261120P770000": 9.4398}}
    assert check(days, truth)["traded"] == {"equal": 2, "compared": 2}


def test_a_day_opend_does_not_hold_is_dropped():
    """OpenD answers a date it does not hold with the latest day, without an error."""
    rows, errors = rank_day(
        FakeOpenD("2026-10-23"), SDK, make_filter, "SPY", date(2025, 10, 15), lambda s: None
    )
    assert rows == {} and "dropped" in errors[0]


def test_the_wanted_list_keeps_the_most_traded_listed_contracts_under_the_day_before():
    days = {
        "2026-10-22": {
            "option_chain": [
                {
                    "options": [
                        {"code": "US.RKLB261120P55000", "prev_close": 5.15, "volume": 808},
                        {"code": "US.RKLB261120P45000", "prev_close": 1.335, "volume": 2192},
                        {"code": "US.RKLB261023P50000", "prev_close": 0.4, "volume": 9999},  # expired
                        {"code": "US.SPY261120P770000", "prev_close": 9.44, "volume": 3042},
                    ]
                }
            ]
        }
    }
    found = wanted(days, ["RKLB"], 1, date(2026, 10, 25))
    assert found == {"2026-10-21": ["US.RKLB261120P45000"]}
