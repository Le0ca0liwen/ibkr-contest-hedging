"""Instructor rules for the SPCX butterfly, RKLB put spread and SPY straddle."""


def prohibited_reason(manifest, contract):
    if manifest.contest_rules != "spcx_rklb_spy":
        return None
    symbol = contract.symbol.upper()
    if symbol == "SPY" and contract.kind == "STK":
        return "Contest prohibits SPY shares (long or short)"
    if contract.kind != "OPT" or contract.expiry != manifest.contest_option_expiry:
        return None
    if symbol == "RKLB" and contract.right == "C":
        return "Contest prohibits RKLB calls at the contest expiry"
    if symbol == "SPY" and contract.con_id not in manifest.mandatory:
        return "Contest prohibits additional SPY options at the contest expiry"
    return None


def validate_portfolio(manifest):
    """Require the seven signed legs; dates/strikes/IDs must come from confirmed inputs."""
    if manifest.contest_option_expiry is None:
        raise ValueError("Contest option expiry must be confirmed explicitly")
    legs = [manifest.contract_map[i] for i in manifest.mandatory]
    if len(legs) != 7 or any(
        c.kind != "OPT" or c.multiplier != 100 or c.expiry != manifest.contest_option_expiry for c in legs
    ):
        raise ValueError("Contest requires seven standard option legs at the confirmed contest expiry")
    grouped = {
        symbol: sorted([c for c in legs if c.symbol.upper() == symbol], key=lambda c: c.strike)
        for symbol in ("SPCX", "RKLB", "SPY")
    }
    butterfly, spread, straddle = (grouped[s] for s in ("SPCX", "RKLB", "SPY"))
    if (
        len(butterfly) != 3
        or any(c.right != "C" for c in butterfly)
        or [manifest.mandatory[c.con_id] for c in butterfly] != [25, -50, 25]
        or not butterfly[0].strike < butterfly[1].strike < butterfly[2].strike
    ):
        raise ValueError("SPCX butterfly requires ascending call strikes with quantities +25/-50/+25")
    if (
        len(spread) != 2
        or any(c.right != "P" for c in spread)
        or [manifest.mandatory[c.con_id] for c in spread] != [-25, 25]
        or not spread[0].strike < spread[1].strike
    ):
        raise ValueError("RKLB bear put spread requires lower-strike -25 and higher-strike +25 puts")
    if (
        len(straddle) != 2
        or {c.right for c in straddle} != {"C", "P"}
        or straddle[0].strike != straddle[1].strike
        or any(manifest.mandatory[c.con_id] != 25 for c in straddle)
    ):
        raise ValueError("SPY straddle requires +25 calls and +25 puts at the same strike")
    for symbol, contracts in grouped.items():
        underlying_ids = {c.underlying_id for c in contracts}
        if len(underlying_ids) != 1:
            raise ValueError(f"{symbol} mandate legs must share one underlying")
        underlying = manifest.contract_map.get(contracts[0].underlying_id)
        if underlying is None or underlying.kind != "STK" or underlying.symbol.upper() != symbol:
            raise ValueError(f"{symbol} mandate requires a matching stock definition for market data")
