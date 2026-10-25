import json

from ops.probe_close_sources import compare, daily_matches, moomoo_code, statement_prices


def test_last_close_is_checked_against_the_previous_statement(tmp_path):
    for day, mark in (("2026-10-22", "6.55"), ("2026-10-23", "5.8989")):
        position = {
            "asset": "OPT",
            "symbol": "RKLB  261120P00055000",
            "expiry": "2026-11-20",
            "put_call": "P",
            "strike": "55",
            "mark": mark,
        }
        (tmp_path / f"{day}.json").write_text(json.dumps({"date": day, "positions": [position]}))
    prices = statement_prices(tmp_path)
    code = moomoo_code("RKLB", "2026-11-20", "P", 55)
    assert code == "US.RKLB261120P55000" and prices["2026-10-23"][code] == 5.8989
    bars = {
        code: [
            {"time_key": "2026-10-23 00:00:00", "close": 5.7, "last_close": 6.55},
            {"time_key": "2026-10-26 00:00:00", "close": 6.1, "last_close": 5.9},
        ]
    }
    rows, matches, checked = compare(bars, prices)
    assert (matches, checked) == (1, 2)  # Oct 23 matches Oct 22's statement; Oct 26 is off
    assert rows[0] == (code, "2026-10-23", 5.7, 6.55, 5.8989, 6.55)


def test_daily_bar_closes_are_counted_against_the_statement_prices():
    prices = {"2026-10-22": {"US.RKLB261120P55000": 6.55}, "2026-10-23": {"US.RKLB261120P55000": 5.8989}}
    bars = {
        "US.RKLB261120P55000": {
            "TRADES": [{"day": "2026-10-22", "close": 6.9}, {"day": "2026-10-23", "close": 5.8989}],
            "MIDPOINT": [{"day": "2026-10-21", "close": 5.4}, {"day": "2026-10-22", "close": 6.975}],
        }
    }
    assert daily_matches(bars, prices) == {"TRADES": (1, 2), "MIDPOINT": (0, 1)}
