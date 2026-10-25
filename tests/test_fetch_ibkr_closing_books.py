import json
from types import SimpleNamespace

from ops.fetch_ibkr_closing_books import (
    chain_wants,
    fetch,
    listed_strikes,
    parse,
    plan,
    statement_wants,
    strike_step,
)


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
    assert chain_wants([chain], ["2026-10-22"], 0.3) == {"2026-10-22": ["US.SPY261120C770000"]}
    assert chain_wants([chain], ["2026-10-22"], 0.3, ["RKLB"]) == {"2026-10-22": []}


def test_the_ladder_takes_the_listed_strikes_when_a_chain_file_lists_them(tmp_path):
    """RKLB Nov 20 lists every dollar from 48: a 2.5 step asked for 52.5 and 57.5, which do not exist."""
    leg = {"asset": "OPT", "symbol": "RKLB  261120P00055000", "expiry": "2026-11-20", "strike": "55"}
    (tmp_path / "2026-10-21.json").write_text(json.dumps({"date": "2026-10-21", "positions": [leg]}))
    chain = tmp_path / "chain.json"
    options = [
        {"code": f"US.RKLB261120{r}{k * 1000}"} for k in (45, 50, 53, 54, 55, 56, 57, 60) for r in "CP"
    ]
    chain.write_text(json.dumps({"options": options}))
    listed = listed_strikes([chain])
    assert listed[("RKLB", "20261120")] == [45, 50, 53, 54, 55, 56, 57, 60]
    wanted = statement_wants(tmp_path, 2, strike_step, listed)["2026-10-21"]
    assert {parse(c)[3] for c in wanted} == {53, 54, 55, 56, 57} and len(wanted) == 10


def test_the_plan_puts_shares_and_statement_legs_first_and_skips_expired_and_fetched_ones():
    legs = {"2026-10-22": {"US.SPY261120C770000", "US.BA261016C195000", "US.SPY261120P770000"}}
    chains = {
        "2026-10-22": ["US.SPY261120C771000", "US.SPY261120C770000"],
        "2026-10-21": ["US.RKLB261120P50000"],
    }
    legs["2026-10-09"] = {"US.BA261016C195000"}  # held then, expired since: asked last
    todo = plan(legs, chains, {("2026-10-22", "US.SPY261120P770000")}, "20261024")
    assert todo == [
        ("2026-10-09", "US.BA"),
        ("2026-10-22", "US.SPY"),
        ("2026-10-22", "US.SPY261120C770000"),
        ("2026-10-21", "US.RKLB"),
        ("2026-10-21", "US.RKLB261120P50000"),  # each day's nearest first, the days side by side
        ("2026-10-22", "US.SPY261120C771000"),
        ("2026-10-09", "US.BA261016C195000"),
    ]


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
    failures = await fetch(FakeIB(), contracts, todo, 4.0, rows.append, sleep, concurrency=2)
    assert rows == [{"day": "2026-10-22", "code": "US.RKLB261120P45000", "bid": 1.85, "ask": 2.01}]
    assert calls[0] == ("rklb", "20261022 16:00:00 US/Eastern", "60 S", "1 min", "BID", True)
    assert (
        set(failures) == {"2026-10-22 US.X261120C1000", "US.Q"} and slept == [0.25] * 3
    )  # no ASK after an empty BID
    calls.clear()
    await fetch(FakeIB(), contracts, todo[:1], 4.0, rows.append, sleep, close="16:15:00")
    assert calls[0] == ("rklb", "20261022 16:15:00 US/Eastern", "60 S", "1 min", "BID", False)  # after hours


async def test_an_expired_option_is_tried_by_symbol_then_with_include_expired_then_by_conid():
    from ops.fetch_ibkr_closing_books import qualify

    seen = []

    class FakeIB:
        async def qualifyContractsAsync(self, *contracts):
            for c in contracts:
                seen.append((c.conId or c.symbol, getattr(c, "includeExpired", False)))
                if c.secType == "STK" or c.conId == 77 or (c.symbol == "SPY" and not c.includeExpired):
                    c.conId = c.conId or 1

    codes = {"US.SPY261120C770000", "US.BA261016C195000", "US.BA261016P195000", "US.BA"}
    found = await qualify(FakeIB(), codes, {"US.BA261016C195000": 77})
    assert set(found) == {"US.SPY261120C770000", "US.BA261016C195000", "US.BA"}
    assert ("BA", True) in seen and (77, False) in seen  # second and third attempts


async def test_contracts_are_asked_in_parallel_within_the_concurrency():
    import asyncio

    open_now, peak, rows = [0], [0], []

    class SlowIB:
        async def reqHistoricalDataAsync(self, contract, *args, **kwargs):
            open_now[0] += 1
            peak[0] = max(peak[0], open_now[0])
            await asyncio.sleep(0.01)
            open_now[0] -= 1
            return [SimpleNamespace(close=1.0)]

    async def no_wait(seconds):
        pass

    todo = [("2026-10-22", f"US.SPY261120C{k}000") for k in range(700, 720)]
    contracts = {code: code for _, code in todo}
    assert await fetch(SlowIB(), contracts, todo, 100.0, rows.append, no_wait, concurrency=5) == {}
    assert len(rows) == 20 and peak[0] == 5


async def test_no_connection_resubscribes_the_account_summary_after_a_reconnect(monkeypatch):
    """IBKR allows two account-summary subscriptions in all: ib_async re-subscribed one on every
    connection at each 1102, and the closing-NAV request was refused (error 322, Oct 24)."""
    import asyncio

    from ib_async import IB

    from hedge.broker import without_summary_resubscribe

    calls = []

    async def subscribe():
        calls.append(1)

    plain, kept = IB(), without_summary_resubscribe(IB())
    for ib in (plain, kept):
        monkeypatch.setattr(ib, "reqAccountSummaryAsync", subscribe)
        ib.errorEvent.emit(-1, 1102, "Connectivity between IB and Trader Workstation has been restored", None)
    await asyncio.sleep(0)
    assert calls == [1]  # only the unmodified connection re-subscribed
