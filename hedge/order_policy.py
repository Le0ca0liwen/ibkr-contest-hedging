"""Deterministic urgency, spread and tick-aware execution pricing."""

from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


def spread_error(manifest, contract, quote):
    policy = manifest.execution
    spread = quote.ask - quote.bid
    absolute = policy.option_spread_absolute if contract.kind == "OPT" else policy.stock_spread_absolute
    relative = policy.option_spread_relative if contract.kind == "OPT" else policy.stock_spread_relative
    if quote.bid <= 0 or quote.mid <= 0 or spread > absolute + 1e-9 or spread / quote.mid > relative + 1e-9:
        return f"Execution spread too wide: {contract.label}"
    return None


def urgency_for(manifest, contract, now, corrective=False, setup=False):
    if setup:
        return "PATIENT"
    if corrective:
        return "CORRECTIVE"
    day = now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
    cutoff = (
        manifest.policy.option_cutoff_minutes
        if contract.kind == "OPT"
        else manifest.policy.stock_cutoff_minutes
    )
    deadline = manifest.session_close(day) - timedelta(minutes=cutoff)
    return "DEADLINE" if (deadline - now).total_seconds() <= 90 else "NORMAL"


def rounded_price(value, tick, signed):
    value, tick = Decimal(str(value)), Decimal(str(tick))
    rounding = ROUND_FLOOR if signed > 0 else ROUND_CEILING
    return (value / tick).quantize(Decimal(1), rounding=rounding) * tick


def choose_order(manifest, broker, contract, quote, signed, now, corrective=False, setup=False):
    error = spread_error(manifest, contract, quote)
    if error:
        raise ValueError(error)
    policy = manifest.execution
    urgency = urgency_for(manifest, contract, now, corrective, setup)
    executable = quote.ask if signed > 0 else quote.bid
    # A real market order is restricted to urgent risk correction in deep,
    # narrowly quoted stock books. Its slippage reserve is modeled, not a cap.
    market = (
        policy.urgent_market_stocks
        and quote.data_type == "live"
        and urgency == "CORRECTIVE"
        and contract.kind == "STK"
        and 0 <= (now - quote.observed_at).total_seconds() <= policy.market_quote_age
        and quote.ask - quote.bid <= policy.market_spread_absolute + 1e-9
        and min(quote.bid_size, quote.ask_size) >= abs(signed) * policy.market_depth_multiple
    )
    value = executable if market or urgency in ("CORRECTIVE", "DEADLINE") else quote.mid
    tick = broker.price_tick(contract.con_id, value) if hasattr(broker, "price_tick") else 0.01
    aggressive = urgency in ("CORRECTIVE", "DEADLINE")
    # A buy rounded down from the ask (or sell rounded up from the bid) can
    # remain passive precisely when a timely fill is needed.
    price = rounded_price(value, tick, -signed if aggressive else signed)
    if not aggressive and float(price) < quote.bid:
        price = rounded_price(quote.bid, tick, -1)
    elif not aggressive and float(price) > quote.ask:
        price = rounded_price(quote.ask, tick, 1)
    if (not aggressive and not quote.bid <= float(price) <= quote.ask) or price <= 0:
        raise ValueError("No valid price tick inside the executable book")
    spread = quote.ask - quote.bid
    reserve = abs(signed) * spread * (0.5 + manifest.policy.slippage_spread_fraction) * contract.multiplier
    if market:
        reserve = max(reserve, abs(signed) * quote.spot * policy.market_slippage_bps / 10000)
    cap = quote.mid + (1 if signed > 0 else -1) * spread * (0.5 + manifest.policy.slippage_spread_fraction)
    if (float(price) - cap) * (1 if signed > 0 else -1) > 1e-9:
        raise ValueError("Executable price tick exceeds modeled price budget")
    return {
        "order_type": "MKT" if market else "LMT",
        "urgency": urgency,
        "price": price,
        "arrival_mid": quote.mid,
        "price_cap": cap,
        "slippage_reserve": reserve,
        "routing_reason": (
            "Urgent stock correction; fresh narrow deep book; market slippage is not capped"
            if market
            else "Marketable limit for risk correction or closing deadline"
            if aggressive
            else "Patient midpoint limit; bounded repricing after wait"
        ),
        "quote": quote.model_dump(mode="json"),
    }
