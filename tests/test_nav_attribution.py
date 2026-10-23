from datetime import timedelta
from decimal import Decimal

import pytest

from hedge.models import Execution, Position
from hedge.valuation import NAVTracker


def test_intervening_fill_does_not_spend_option_premium_as_nav_loss(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    assert tracker.estimate(snapshot, quotes, manifest.contract_map) == 1000000
    price = Decimal(str(quotes[103].ask))
    e = Execution(
        exec_id="fill.01",
        order_id=1,
        account=manifest.account,
        con_id=103,
        signed_quantity=1,
        price=price,
        at=now + timedelta(seconds=1),
    )
    tracker.execution(e)
    changed = snapshot.model_copy(
        update={
            "at": now + timedelta(seconds=2),
            "positions": (*snapshot.positions, Position(con_id=103, quantity=1)),
        }
    )
    expected_loss = (quotes[103].ask - quotes[103].mid) * 100 + 1
    assert tracker.estimate(changed, quotes, manifest.contract_map) == pytest.approx(1000000 - expected_loss)
    tracker.fee(e.exec_id, "1.20")
    assert tracker.estimate(changed, quotes, manifest.contract_map) == pytest.approx(
        1000000 - expected_loss - 0.2
    )
    updated = changed.model_copy(
        update={"nav": Decimal(str(1000000 - expected_loss - 0.2)), "account_at": now + timedelta(seconds=3)}
    )
    assert tracker.estimate(updated, quotes, manifest.contract_map) == pytest.approx(
        1000000 - expected_loss - 0.2
    )


def test_inventory_change_without_fill_cannot_authorize_score_control(manifest, snapshot, quotes):
    tracker = NAVTracker(manifest)
    tracker.estimate(snapshot, quotes, manifest.contract_map)
    changed = snapshot.model_copy(
        update={"positions": (*snapshot.positions, Position(con_id=10, quantity=100))}
    )
    with pytest.raises(ValueError, match="attribution"):
        tracker.estimate(changed, quotes, manifest.contract_map)


def stream(snapshot, now, seconds, pnl, nav=None, pushed=None, realized=None):
    """A snapshot `seconds` after `now` carrying daily P&L `pnl` (read just now)."""
    at = now + timedelta(seconds=seconds)
    update = {"at": at, "pnl_daily": Decimal(str(pnl)), "pnl_at": at}
    if realized is not None:
        update["pnl_realized"] = Decimal(str(realized))
    if nav is not None:
        update.update(nav=Decimal(str(nav)), account_at=now + timedelta(seconds=pushed))
    return snapshot.model_copy(update=update)


def test_the_pnl_stream_rolls_the_settled_nav_forward_every_second(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    first = stream(snapshot, now, 1, -500.0)  # push at `now`, P&L read 1 s later: settled
    assert tracker.estimate(first, quotes, manifest.contract_map) == pytest.approx(1_000_000)
    assert tracker.source == "IBKR P&L stream (1 s)"
    later = stream(snapshot, now, 2, -380.5)
    assert tracker.estimate(later, quotes, manifest.contract_map) == pytest.approx(1_000_119.5)


def test_a_nav_push_right_after_a_fill_is_never_adopted(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    tracker.estimate(stream(snapshot, now, 1, 0.0), quotes, manifest.contract_map)
    tracker.execution(
        Execution(
            exec_id="burn.01",
            order_id=1,
            account=manifest.account,
            con_id=10,
            signed_quantity=200,
            price=Decimal("195.01"),
            at=now + timedelta(seconds=100),
        )
    )
    # The fill's cost reaches the P&L stream (-6); IBKR's push 2 s after the fill is off by +900.
    spiked = stream(snapshot, now, 103, -6.0, nav=1_000_894, pushed=102)
    assert tracker.estimate(spiked, quotes, manifest.contract_map) == pytest.approx(1_000_000 - 6)
    # Three quiet minutes later a settled push is adopted, and the check records the gap.
    quiet = stream(snapshot, now, 290, -10.0, nav=999_991, pushed=285)
    assert tracker.estimate(quiet, quotes, manifest.contract_map) == pytest.approx(999_991)
    assert tracker.last_check["difference"] == pytest.approx(1.0)


def test_a_daily_pnl_reset_keeps_the_estimate_continuous(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    tracker.estimate(stream(snapshot, now, 1, 2_400.0), quotes, manifest.contract_map)
    tracker.estimate(stream(snapshot, now, 40, 2_450.0), quotes, manifest.contract_map)
    reset = stream(snapshot, now, 41, 0.0)  # IBKR starts a new P&L day
    assert tracker.estimate(reset, quotes, manifest.contract_map) == pytest.approx(1_000_050)
    after = stream(snapshot, now, 42, 12.0)
    assert tracker.estimate(after, quotes, manifest.contract_map) == pytest.approx(1_000_062)


def test_a_stale_stream_falls_back_to_fill_attribution(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    tracker.estimate(stream(snapshot, now, 1, 0.0), quotes, manifest.contract_map)
    stale = snapshot.model_copy(
        update={
            "at": now + timedelta(seconds=30),
            "pnl_daily": Decimal("50"),
            "pnl_at": now + timedelta(seconds=1),
        }
    )
    assert tracker.estimate(stale, quotes, manifest.contract_map) == pytest.approx(1_000_000)
    assert tracker.source == "fill attribution"


def test_the_broker_records_the_account_pnl_stream(manifest):
    import asyncio
    from types import SimpleNamespace

    from hedge.broker import IBKRBroker

    broker = IBKRBroker(manifest, asyncio.Queue())
    broker._pnl(SimpleNamespace(account="DU_OTHER", dailyPnL=5.0))
    broker._pnl(SimpleNamespace(account=manifest.account, dailyPnL=float("nan")))
    broker._pnl(SimpleNamespace(account=manifest.account, dailyPnL=1.7976931348623157e308))  # IBKR "unset"
    assert broker.pnl_daily is None
    broker._pnl(SimpleNamespace(account=manifest.account, dailyPnL=-123.45, realizedPnL=-6_131.77))
    assert broker.pnl_daily == Decimal("-123.45") and broker.pnl_at is not None
    assert broker.pnl_realized == Decimal("-6131.77")
    broker._pnl(SimpleNamespace(account=manifest.account, dailyPnL=5.0, realizedPnL=1.7976931348623157e308))
    assert broker.pnl_daily == Decimal("5.0") and broker.pnl_realized is None


def test_a_large_jump_is_not_rebased_away_but_waits_for_a_later_push(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    tracker.estimate(stream(snapshot, now, 1, 100.0), quotes, manifest.contract_map)
    jump = stream(snapshot, now, 2, 5_100.0)  # not a drop to zero, no fill, realized unchanged
    assert tracker.estimate(jump, quotes, manifest.contract_map) == pytest.approx(1_000_000)
    assert tracker.source == "fill attribution"
    # The push the old base came from (paired with the old P&L) is not re-adopted ...
    old_push = stream(snapshot, now, 30, 5_110.0)
    assert tracker.estimate(old_push, quotes, manifest.contract_map) == pytest.approx(1_000_000)
    assert tracker.source == "fill attribution"
    # ... a push from after the jump decides: here NAV really moved.
    pushed = stream(snapshot, now, 182, 5_120.0, nav=1_005_020, pushed=180)
    assert tracker.estimate(pushed, quotes, manifest.contract_map) == pytest.approx(1_005_020)
    assert tracker.source == "IBKR P&L stream (1 s)"


def test_ibkrs_pnl_day_roll_keeps_the_estimate_continuous(manifest, snapshot, quotes, now):
    """Oct 22: realized -6,131.77 -> 0 lifted the daily P&L -496.04 -> +5,631.17; NAV held."""
    tracker = NAVTracker(manifest)
    tracker.estimate(stream(snapshot, now, 1, -496.04, realized=-6_131.77), quotes, manifest.contract_map)
    rolled = stream(snapshot, now, 2, 5_631.17, realized=0.0)
    assert tracker.estimate(rolled, quotes, manifest.contract_map) == pytest.approx(1_000_000 - 4.56)
    assert tracker.source == "IBKR P&L stream (1 s)"
    later = stream(snapshot, now, 3, 5_641.17, realized=0.0)
    assert tracker.estimate(later, quotes, manifest.contract_map) == pytest.approx(1_000_000 + 5.44)
    # The next push is paired with the new day's P&L and agrees.
    pushed = stream(snapshot, now, 181, 5_641.17, nav=1_000_005.44, pushed=180, realized=0.0)
    assert tracker.estimate(pushed, quotes, manifest.contract_map) == pytest.approx(1_000_005.44)
    assert tracker.last_check["difference"] == pytest.approx(0.0, abs=1e-6)


def test_a_fill_that_zeroes_realized_pnl_is_not_a_roll(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    tracker.estimate(stream(snapshot, now, 1, -40.0, realized=-300.0), quotes, manifest.contract_map)
    tracker.execution(
        Execution(
            exec_id="close.01",
            order_id=1,
            account=manifest.account,
            con_id=10,
            signed_quantity=-10,
            price=Decimal("195"),
            at=now + timedelta(seconds=2),
        )
    )
    filled = stream(snapshot, now, 3, -42.0, realized=0.0)  # the fill realized +300; P&L moved -2
    assert tracker.estimate(filled, quotes, manifest.contract_map) == pytest.approx(1_000_000 - 2)


def test_a_stream_gap_waits_for_a_fresh_settled_push(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    tracker.estimate(stream(snapshot, now, 1, 0.0), quotes, manifest.contract_map)
    stale = snapshot.model_copy(
        update={
            "at": now + timedelta(seconds=60),
            "pnl_daily": Decimal("0"),
            "pnl_at": now + timedelta(seconds=1),
        }
    )
    tracker.estimate(stale, quotes, manifest.contract_map)
    # The stream resumes 10 minutes later, 3,000 higher: no base until a settled push.
    resumed = stream(snapshot, now, 600, 3_000.0)
    assert tracker.estimate(resumed, quotes, manifest.contract_map) == pytest.approx(1_000_000)
    assert tracker.source == "fill attribution"
    pushed = stream(snapshot, now, 601, 3_001.0, nav=1_003_000.0, pushed=600)
    assert tracker.estimate(pushed, quotes, manifest.contract_map) == pytest.approx(1_003_000)
    assert tracker.source == "IBKR P&L stream (1 s)"


def test_after_hours_the_stream_adds_the_held_options_model_revaluation(manifest, snapshot, quotes):
    from datetime import datetime

    from hedge.config import NY

    m = manifest.model_copy(update={"contest_rules": "spcx_rklb_spy"})
    evening = datetime(2026, 10, 7, 17, tzinfo=NY)
    modeled = {
        i: q.model_copy(update={"data_type": "model" if m.contract_map[i].kind == "OPT" else "live"})
        for i, q in quotes.items()
    }
    base = snapshot.model_copy(
        update={"at": evening, "account_at": evening, "pnl_daily": Decimal("0"), "pnl_at": evening}
    )
    tracker = NAVTracker(m)
    assert tracker.estimate(base, modeled, m.contract_map) == pytest.approx(1_000_000)
    held = next(p for p in snapshot.positions if m.contract_map[p.con_id].kind == "OPT")
    moved = {
        **modeled,
        held.con_id: modeled[held.con_id].model_copy(
            update={"bid": modeled[held.con_id].bid + 1, "ask": modeled[held.con_id].ask + 1}
        ),
    }
    later = base.model_copy(
        update={"at": evening + timedelta(seconds=2), "pnl_at": evening + timedelta(seconds=2)}
    )
    expected = 1_000_000 + held.quantity * m.contract_map[held.con_id].multiplier * 1.0
    assert tracker.estimate(later, moved, m.contract_map) == pytest.approx(expected)


def test_the_push_is_paired_with_the_pnl_when_it_arrived(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    first = stream(snapshot, now, 25, 80.0).model_copy(update={"account_pnl_daily": Decimal("50")})
    # NetLiquidation (1,000,000) arrived with P&L 50; P&L is 80 now: the estimate is 30 higher.
    assert tracker.estimate(first, quotes, manifest.contract_map) == pytest.approx(1_000_030)


def test_an_older_push_is_never_adopted_after_a_newer_one(manifest, snapshot, quotes, now):
    tracker = NAVTracker(manifest)
    newer = stream(snapshot, now, 200, 10.0, nav=1_000_010, pushed=195)
    assert tracker.estimate(newer, quotes, manifest.contract_map) == pytest.approx(1_000_010)
    # A caller still holding a snapshot from before that push (account_at = now) must not step back.
    older = stream(snapshot, now, 201, 12.0)
    assert tracker.estimate(older, quotes, manifest.contract_map) == pytest.approx(1_000_012)


async def test_the_dashboard_reads_a_fresh_snapshot_between_monitor_passes(manifest, store, quotes, now):
    from hedge.service import TradingService

    service = TradingService(manifest, store, demo=True)
    service.now = lambda: now
    await service.broker.connect()
    fresh = {i: q.model_copy(update={"data_type": "synthetic"}) for i, q in quotes.items()}
    service.broker.update_quotes(fresh, now)
    service.last_snapshot, service.last_quotes = service.broker.snapshot(), fresh
    before = service.state()["nav_estimate"]
    service.broker.nav += Decimal("123")  # IBKR's account moved; the monitor has not run yet
    after = service.state()
    assert after["nav_estimate"] == pytest.approx(before + 123)
    assert Decimal(after["nav"]) == service.broker.nav
