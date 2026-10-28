from datetime import datetime, timedelta

import pytest

from hedge.models import Position
from hedge.scenarios import ScenarioEngine
from ops.replay_limits import _archive_lines, replay_point, trade_value, variant_manifests


def test_every_gross_variant_is_solved_valued_and_d_falls_back_to_a(manifest, snapshot, quotes, now):
    held = {**snapshot.quantities(), 104: -20}
    snap = snapshot.model_copy(
        update={"positions": tuple(Position(con_id=i, quantity=q) for i, q in held.items())}
    )
    sc = ScenarioEngine(manifest).build(snap, quotes, now)
    manifests = variant_manifests(manifest)
    assert manifests["B"].risk.gross_notional == 3.5 and manifests["C"].risk.gross_basis == "delta"
    closes = {i: q.mid + 0.5 for i, q in quotes.items()}
    target = manifest.initial_nav
    results = replay_point(manifests, snap, quotes, sc, target, float(target), "dynamic_l1", closes)
    for name in "ABCD":
        assert results[name]["status"] == "OPTIMAL" and results[name]["risk_passed"]
        trades = results[name]["trades"]
        assert results[name]["value_to_close"] is not None or trades
    # Far from 3x gross, B never needs the exception: D is A's target.
    assert results["D"]["from"] == "A" and results["D"]["trades"] == results["A"]["trades"]
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
