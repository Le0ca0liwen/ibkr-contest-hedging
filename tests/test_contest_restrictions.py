from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from hedge.config import Manifest
from hedge.models import Contract, Position, Quote, WorkingOrder
from hedge.risk import MandateRegistry, RiskGate
from hedge.setup import SetupPlanner
from hedge.universe import screen_options


@pytest.fixture
def contest(manifest):
    expiry = date(2026, 10, 16)  # Synthetic test date, not an instructor-confirmed expiry.
    stocks = [Contract(con_id=i, symbol=s, kind="STK") for i, s in [(10, "SPCX"), (20, "RKLB"), (30, "SPY")]]
    specs = [
        (101, 10, 90, "C"),
        (102, 10, 100, "C"),
        (103, 10, 110, "C"),
        (104, 20, 90, "P"),
        (105, 20, 100, "P"),
        (106, 30, 100, "C"),
        (107, 30, 100, "P"),
        (201, 20, 110, "C"),
        (202, 30, 110, "C"),
        (203, 30, 90, "P"),
        (204, 10, 120, "C"),
    ]
    symbols = {s.con_id: s.symbol for s in stocks}
    options = [
        Contract(
            con_id=i,
            symbol=symbols[u],
            kind="OPT",
            multiplier=100,
            expiry=expiry,
            strike=k,
            right=r,
            underlying_id=u,
        )
        for i, u, k, r in specs
    ]
    options += [
        options[-3].model_copy(update={"con_id": 301, "expiry": expiry + timedelta(days=7)}),
        options[-4].model_copy(update={"con_id": 302, "expiry": expiry + timedelta(days=7)}),
    ]
    data = manifest.model_dump()
    data.update(
        contracts=stocks + options,
        mandatory={101: 25, 102: -50, 103: 25, 104: -25, 105: 25, 106: 25, 107: 25},
        allowlist=(10, 20, 204, 301, 302),
        contest_rules="spcx_rklb_spy",
        contest_option_expiry=expiry,
    )
    return Manifest.model_validate(data)


def frame(contest, snapshot, now):
    snap = snapshot.model_copy(
        update={"positions": tuple(Position(con_id=i, quantity=q) for i, q in contest.mandatory.items())}
    )
    quotes = {
        c.con_id: Quote(
            con_id=c.con_id,
            bid=1,
            ask=1.01,
            bid_size=1000,
            ask_size=1000,
            observed_at=now,
            greek_at=now,
            iv=0.2,
            spot=100,
            delta=0,
            shortable_shares=10000,
        )
        for c in contest.contracts
    }
    ids = tuple(quotes)
    scenarios = SimpleNamespace(
        ids=ids, stress=np.zeros((2, len(ids))), changes=np.zeros((2, len(ids))), rhos={}, betas={}
    )
    return snap, quotes, scenarios


def test_signed_mandates_and_permitted_hedges(contest, snapshot, now):
    snap, quotes, scenarios = frame(contest, snapshot, now)
    assert MandateRegistry(contest).check(snap.quantities()) == []
    for i in contest.allowlist:
        for sign in (-1, 1):
            result = RiskGate(contest).validate(snap, quotes, scenarios, {**snap.quantities(), i: sign}, now)
            assert result.passed, result.reasons
    assert not contest.prohibited_reason(contest.contract_map[106])
    assert not contest.prohibited_reason(contest.contract_map[107])


@pytest.mark.parametrize("identifier", [30, 201, 202, 203])
def test_forbidden_allowlist_rejected(contest, identifier):
    data = contest.model_dump()
    data["allowlist"] = (*contest.allowlist, identifier)
    with pytest.raises(ValueError, match="Forbidden allowlist"):
        Manifest.model_validate(data)


@pytest.mark.parametrize("identifier", [30, 201, 202, 203])
@pytest.mark.parametrize("sign", [-1, 1])
def test_gate_blocks_both_directions_even_with_bad_allowlist_and_corrective_mode(
    contest, snapshot, now, identifier, sign
):
    # Bypass config validation deliberately to exercise the independent transmission gate.
    unsafe = contest.model_copy(update={"allowlist": (*contest.allowlist, identifier)})
    snap, quotes, scenarios = frame(unsafe, snapshot, now)
    target = {**snap.quantities(), identifier: sign}
    gate = RiskGate(unsafe)
    for corrective in (False, True):
        assert any(
            "Contest prohibits" in r
            for r in gate.validate(snap, quotes, scenarios, target, now, corrective=corrective).reasons
        )
    held = snap.model_copy(
        update={"positions": (*snap.positions, Position(con_id=identifier, quantity=sign))}
    )
    assert any("Contest prohibits" in r for r in gate.validate(held, quotes, scenarios, target, now).reasons)
    working = snap.model_copy(
        update={"orders": (WorkingOrder(order_id=1, con_id=identifier, signed_remaining=sign),)}
    )
    assert any(
        "Forbidden working order" in r
        for r in gate.validate(working, quotes, scenarios, snap.quantities(), now).reasons
    )
    assert identifier not in screen_options(unsafe, quotes, {})
    assert identifier in screen_options(unsafe, quotes, target)  # Held risk must remain visible.


@pytest.mark.parametrize("identifier", [101, 102, 103, 104, 105, 106, 107])
def test_mandates_cannot_be_offset_or_increased(contest, snapshot, now, identifier):
    snap, quotes, scenarios = frame(contest, snapshot, now)
    for change in (-1, 1):
        target = {**snap.quantities(), identifier: snap.quantities()[identifier] + change}
        result = RiskGate(contest).validate(snap, quotes, scenarios, target, now, corrective=True)
        assert any("Mandatory conId" in r for r in result.reasons)


@pytest.mark.parametrize("change", ["expiry", "quantity", "direction", "strike", "underlying", "profile"])
def test_invalid_contest_structure_fails_closed(contest, change):
    data = contest.model_dump()
    data["contracts"] = list(data["contracts"])
    if change == "expiry":
        data["contest_option_expiry"] = None
    if change == "quantity":
        data["mandatory"][102] = -49
    if change == "direction":
        data["mandatory"][104] = 25
    if change == "strike":
        data["contracts"][9]["strike"] = 105
    if change == "underlying":
        data["contracts"][3]["underlying_id"] = 20
    if change == "profile":
        data["contest_rules"] = "practice"
    with pytest.raises(ValueError):
        Manifest.model_validate(data)


def test_short_mandates_include_assignment_exposure_and_setup_is_blocked(contest, snapshot, now):
    snap, quotes, scenarios = frame(contest, snapshot, now)
    gate = RiskGate(contest)
    risk = gate.validate(snap, quotes, scenarios, snap.quantities(), now)
    assert risk.metrics["assignment_cash"] == 50 * 100 * 100 + 25 * 100 * 90
    with pytest.raises(ValueError, match="Automatic contest setup is unsupported"):
        SetupPlanner(contest, gate).plan(snap, quotes, scenarios, now)


def test_setup_direction_checks_for_short_mandates(contest, snapshot, now):
    at = contest.session_open(contest.purchase_session) + timedelta(minutes=10)
    snap, quotes, scenarios = frame(contest, snapshot, at)
    snap = snap.model_copy(update={"at": at, "account_at": at, "positions": ()})
    gate = RiskGate(contest)
    target = {102: -1}
    result = gate.validate(snap, quotes, scenarios, target, at, day_zero=True)
    assert result.passed, result.reasons
    assert result.metrics["estimated_added_margin"] > 0
    for quantity in (1, -51):
        result = gate.validate(snap, quotes, scenarios, {102: quantity}, at, day_zero=True)
        assert any("required direction" in reason for reason in result.reasons)
    partial = snap.model_copy(update={"positions": (Position(con_id=102, quantity=-2),)})
    result = gate.validate(partial, quotes, scenarios, target, at, day_zero=True)
    assert any("required direction" in reason for reason in result.reasons)


async def test_legacy_package_submission_cannot_start_contest_setup(contest, store):
    from hedge.execution import ExecutionManager

    # No broker is supplied: rejecting this unsupported path must not call one.
    manager = ExecutionManager(contest, store, None, RiskGate(contest))
    manager.paused = False
    result = await manager.submit_package(101, 204, "decision", None, {}, None, None)
    assert not result["submitted"] and "unsupported" in result["reason"]
    assert not store.intents(contest.account)


def test_contest_template_cannot_activate_without_confirmed_contracts():
    from hedge.config import load_manifest

    with pytest.raises(ValueError):
        load_manifest("config/contest.template.yaml")
