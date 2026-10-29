from datetime import datetime, timedelta

import pytest

from hedge.models import Position
from hedge.scenarios import ScenarioEngine
from ops.replay_limits import _archive_lines, replay_point, trade_value, variant_manifests


def test_every_gross_variant_is_solved_valued_and_risk_checked(manifest, snapshot, quotes, now):
    held = {**snapshot.quantities(), 104: -20}
    snap = snapshot.model_copy(
        update={"positions": tuple(Position(con_id=i, quantity=q) for i, q in held.items())}
    )
    sc = ScenarioEngine(manifest).build(snap, quotes, now)
    manifests = variant_manifests(manifest)
    assert set(manifests) == {"A", "B", "C", "D", "C+D", "A-STK", "C-STK", "D-STK", "O"}
    assert manifests["B"].risk.gross_notional == 3.5 and manifests["C"].risk.gross_basis == "delta"
    assert manifests["D"].risk.gross_risk_reducing == 3.5 and manifests["C+D"].risk.gross_basis == "delta"
    closes = {i: q.mid + 0.5 for i, q in quotes.items()}
    target = manifest.initial_nav
    later = {i: c + 1.0 for i, c in closes.items()}
    results = replay_point(manifests, snap, quotes, sc, target, float(target), "dynamic_l1", closes, later)
    for name in manifests:
        assert results[name]["status"] == "OPTIMAL" and results[name]["risk_passed"]
        assert results[name]["value_to_close"] is not None and results[name]["solve_ms"] > 0
    for name in ("A-STK", "C-STK", "D-STK"):  # shares only
        assert all(" " not in label for label in results[name]["trades"])
    a = results["A"]
    traded = sum(q * (1 if " " not in k else 100) for k, q in a["trades"].items())
    assert a["next_value"] == pytest.approx(traded)  # every close one dollar higher the next session
    assert a["value_to_close"] == pytest.approx(a["market"] - a["cost"]) and a["cost"] >= 0
    assert a["eligible"] == (a["improvement"] > a["threshold"] and a["risk_passed"])
    # Far below 3x gross, D's second solve never runs and its target is A's.
    assert results["D"]["risk_reducing_solve"] is None and results["D"]["trades"] == results["A"]["trades"]
    assert results["current"]["gross"] < 3 * manifest.initial_nav


def test_a_trade_is_valued_from_its_quotes_to_the_close_less_its_cost(manifest, quotes):
    closes = {10: quotes[10].mid + 1.0}
    value = trade_value(manifest, quotes, closes, {10: -100})
    assert value < -100 + 1e-9 and value > -100 - 10  # -$100 of price, a few dollars of cost
    assert trade_value(manifest, quotes, {}, {10: 1}) is None


def test_archive_lines_are_the_last_received_before_each_moment(tmp_path):
    at = datetime(2026, 10, 28, 17, 0, tzinfo=datetime.now().astimezone().tzinfo).astimezone()
    lines = [
        f'{{"received_at": "{(at + timedelta(seconds=5 * k)).isoformat()}", "k": {k}}}\n'.encode()
        for k in range(10)
    ]
    path = tmp_path / "raw-20261028.jsonl"
    path.write_bytes(b"".join(lines))
    found = _archive_lines(path, [at + timedelta(seconds=12), at + timedelta(minutes=5)])
    assert b'"k": 2' in found[at + timedelta(seconds=12)]
    assert at + timedelta(minutes=5) not in found  # nothing within 60 s before it
    with pytest.raises(KeyError):
        found[at]
