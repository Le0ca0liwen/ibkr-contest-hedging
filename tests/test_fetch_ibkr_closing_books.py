import json
from types import SimpleNamespace

from ops.fetch_ibkr_closing_books import chain_wants, fetch, plan, statement_wants, strike_step


def test_statement_legs_come_with_their_chain_and_chains_with_every_day(tmp_path):
    leg = {"asset": "OPT", "symbol": "BA    261016C00195000", "expiry": "2026-10-16", "strike": "195"}
    (tmp_path / "2026-10-09.json").write_text(json.dumps({"date": "2026-10-09", "positions": [leg]}))
    wanted = statement_wants(tmp_path, 1, strike_step)
    assert wanted["2026-10-09"] == {f"US.BA261016{r}{k}" for r in "CP" for k in (192500, 195000, 197500)}
    chain = tmp_path / "chain.json"
    chain.write_text(
        json.dumps(
            {
                "underlyings": {"SPY": {"spot": 760.0}},
                "options": [
                    {"code": "US.SPY261120C770000", "underlying": "SPY", "strike": 770.0},
                    {"code": "US.SPY261120C500000", "underlying": "SPY", "strike": 500.0},
                ],
            }
        )
    )
    assert chain_wants([chain], ["2026-10-22"], 0.3) == {"2026-10-22": {"US.SPY261120C770000"}}


def test_the_plan_skips_expired_options_and_books_on_file_and_adds_the_shares():
    wanted = {"2026-10-22": {"US.SPY261120C770000", "US.BA261016C195000", "US.SPY261120P770000"}}
    todo = plan(wanted, {("2026-10-22", "US.SPY261120P770000")})
    assert todo == [("2026-10-22", "US.SPY261120C770000"), ("2026-10-22", "US.SPY")]


async def test_each_book_is_the_1559_bid_and_ask_bar_closes_at_a_bounded_rate():
    calls, slept, rows = [], [], []

    class FakeIB:
        async def reqHistoricalDataAsync(self, contract, end, duration, bar, side, **kwargs):
            calls.append((contract, end, duration, bar, side, kwargs["useRTH"]))
            if contract == "dead":
                return []
            return [SimpleNamespace(close={"BID": 1.85, "ASK": 2.01}[side])]

    async def sleep(seconds):
        slept.append(seconds)

    contracts = {"US.RKLB261120P45000": "rklb", "US.X261120C1000": "dead"}
    todo = [("2026-10-22", "US.RKLB261120P45000"), ("2026-10-22", "US.X261120C1000"), ("2026-10-22", "US.Q")]
    failures = await fetch(FakeIB(), contracts, todo, 4.0, rows.append, sleep)
    assert rows == [{"day": "2026-10-22", "code": "US.RKLB261120P45000", "bid": 1.85, "ask": 2.01}]
    assert calls[0] == ("rklb", "20261022 16:00:00 US/Eastern", "60 S", "1 min", "BID", True)
    assert set(failures) == {"2026-10-22 US.X261120C1000", "US.Q"} and slept == [0.25] * 4
