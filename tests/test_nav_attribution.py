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


def stream(snapshot, now, seconds, pnl, nav=None, pushed=None):
    """A snapshot `seconds` after `now` carrying daily P&L `pnl` (read just now)."""
    at = now + timedelta(seconds=seconds)
    update = {"at": at, "pnl_daily": Decimal(str(pnl)), "pnl_at": at}
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
    assert broker.pnl_daily is None
    broker._pnl(SimpleNamespace(account=manifest.account, dailyPnL=-123.45))
    assert broker.pnl_daily == Decimal("-123.45") and broker.pnl_at is not None
