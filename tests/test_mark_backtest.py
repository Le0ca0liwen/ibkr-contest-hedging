import json
import math

from hedge.occ_marks import _Tree
from ops.mark_backtest import load_books, load_truth, main, parse_code


def test_the_backtest_scores_every_estimator_and_tunes_leave_one_day_out(tmp_path):
    """Two days of a synthetic SPCX chain whose statement prices are the curve itself: a wide quote
    off the curve makes the midpoint wrong and the OCC rebuild closer."""
    statements = tmp_path / "statements"
    statements.mkdir()
    days = {}
    for day, spot in (("2026-10-22", 100.0), ("2026-10-23", 102.0)):
        tree = _Tree(spot, 0.034, 0.0, 29, 201)
        archive, positions = {}, []
        for k in range(80, 121, 5):
            vol = 0.5 + 0.4 * math.log(k / spot) ** 2
            for right in "CP":
                price = tree.price(k, right, vol)
                code = f"US.SPCX261120{right}{k * 1000}"
                wide = (k, right) == (105, "P")
                archive[code] = {
                    "bid": round(price - 0.05, 2),
                    "ask": round(price + (0.8 if wide else 0.05), 2),
                }
                if k in (100, 105):
                    positions.append(
                        {
                            "asset": "OPT",
                            "symbol": "SPCX",
                            "conid": k * 10 + (right == "P"),
                            "put_call": right,
                            "strike": k,
                            "expiry": "2026-11-20",
                            "quantity": "25",
                            "mark": round(price, 4),
                        }
                    )
        archive["US.SPCX"] = {"bid": spot - 0.01, "ask": spot + 0.01}
        days[day] = {"archive": {"16:00": archive}}
        (statements / f"{day}.json").write_text(json.dumps({"date": day, "positions": positions}))
    (tmp_path / "dataset.json").write_text(json.dumps({"days": days}))
    truth, conids = load_truth(statements, json.loads((tmp_path / "dataset.json").read_text()))
    books, spots = load_books(json.loads((tmp_path / "dataset.json").read_text()), conids, [])
    assert len(truth["2026-10-22"]) == 4 and spots["2026-10-23"]["SPCX"] == 102.0
    assert parse_code("US.SPCX261120P105000")[3] == 105.0
    out = tmp_path / "report.json"
    main(["--statements", str(statements), "--dataset", str(tmp_path / "dataset.json"), "--output", str(out)])
    report = json.loads(out.read_text())
    mid = report["summary"]["mid "]
    best_occ = min(
        (v for k, v in report["summary"].items() if k.startswith("occ")), key=lambda v: v["day_value_mae"]
    )
    assert best_occ["day_value_mae"] < mid["day_value_mae"]  # the wide put pulled back toward the curve
    assert set(report["held_out"]) == {"2026-10-22", "2026-10-23"}
    assert all({"occ", "mid", "local"} <= set(day) for day in report["held_out"].values())


def test_prev_close_prices_are_truth_for_every_listed_code(tmp_path):
    """Moomoo's prev_close_price is the clearing house's price (= the statement's Close Price): a
    chain taken on Monday scores Friday, and a chain file taken before the roll scores its day."""
    dataset = {
        "days": {
            "2026-10-26": {
                "option_chain": [
                    {
                        "phase": "pre_close",
                        "options": [
                            {"code": "US.SPCX261120C150000", "bid": 1, "ask": 2, "prev_close": 1.4321},
                            {"code": "US.SPCX261120C155000", "bid": 1, "ask": 2, "prev_close": None},
                        ],
                    }
                ]
            }
        }
    }
    statements = tmp_path / "statements"
    statements.mkdir()
    position = {
        "asset": "OPT",
        "symbol": "SPCX",
        "conid": 7,
        "put_call": "C",
        "strike": 150,
        "expiry": "2026-11-20",
        "quantity": "25",
        "mark": 1.43,
    }
    (statements / "2026-10-23.json").write_text(json.dumps({"date": "2026-10-23", "positions": [position]}))
    chain_file = tmp_path / "chain.json"
    chain_file.write_text(json.dumps({"options": [{"code": "US.RKLB261120P55000", "prev_close": 6.55}]}))
    truth, _ = load_truth(statements, dataset, [(str(chain_file), "2026-10-22")])
    # The statement's own price (with the position) wins over Moomoo's for the same code.
    assert truth["2026-10-23"] == {"US.SPCX261120C150000": {"mark": 1.43, "quantity": 25.0}}
    assert truth["2026-10-22"] == {"US.RKLB261120P55000": {"mark": 6.55, "quantity": 0.0}}


def test_spy_style_options_use_their_1615_books_unless_asked_for_1600():
    dataset = {
        "days": {
            "2026-10-22": {
                "archive": {
                    "16:00": {
                        "US.SPY261120C770000": {"bid": 10.68, "ask": 10.74},
                        "US.SPY": {"bid": 759.0, "ask": 759.2},
                        "US.RKLB261120P55000": {"bid": 6.55, "ask": 7.40},
                    },
                    "16:15": {
                        "US.SPY261120C770000": {"bid": 10.71, "ask": 10.77},
                        "US.SPY": {"bid": 760.0, "ask": 760.2},
                        "US.RKLB261120P55000": {"bid": 6.0, "ask": 8.0},
                    },
                }
            }
        }
    }
    books, spots = load_books(dataset, {}, [])
    day = books["2026-10-22"]
    assert day["US.SPY261120C770000"] == (10.71, 10.77) and day["US.RKLB261120P55000"] == (6.55, 7.40)
    assert spots["2026-10-22"]["SPY"] == 760.1
    books, spots = load_books(dataset, {}, [], late_at="16:00")
    assert books["2026-10-22"]["US.SPY261120C770000"] == (10.68, 10.74)
    assert spots["2026-10-22"]["SPY"] == 759.1
