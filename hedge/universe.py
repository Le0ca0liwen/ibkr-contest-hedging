def screen_snapshot(manifest, quotes, snapshot):
    """Retain filled inventory AND working legs, including orders closing to zero."""
    retained = snapshot.quantities()
    for order in snapshot.orders:
        if order.signed_remaining:
            retained.setdefault(order.con_id, order.signed_remaining)
    return screen_options(manifest, quotes, retained)


def screen_options(manifest, quotes, actual):
    """Screen by moneyness/expiry deterministically, preserving held hedges."""
    chosen = {i for i in actual if actual[i]} | set(manifest.mandatory)
    approved = {i for i in manifest.allowlist if not manifest.prohibited_reason(manifest.contract_map[i])}
    chosen |= {i for i in approved if manifest.contract_map[i].kind == "STK"}
    for symbol in sorted({c.symbol for c in manifest.contracts}):
        options = [
            manifest.contract_map[i]
            for i in approved
            if manifest.contract_map[i].kind == "OPT"
            and manifest.contract_map[i].symbol == symbol
            and i in quotes
        ]
        mandatory_expiries = {
            manifest.contract_map[i].expiry
            for i in manifest.mandatory
            if manifest.contract_map[i].symbol == symbol and manifest.contract_map[i].kind == "OPT"
        }
        primary = sorted(mandatory_expiries)[0] if mandatory_expiries else None
        expiries = sorted({c.expiry for c in options})
        later = next((e for e in expiries if primary is None or e > primary), None)
        same = [c for c in options if c.expiry == primary]
        following = [c for c in options if c.expiry == later]
        same.sort(key=lambda c: (abs(c.strike / quotes[c.con_id].spot - 1), c.con_id))
        following.sort(key=lambda c: (abs(c.strike / quotes[c.con_id].spot - 1), c.con_id))
        chosen.update(c.con_id for c in same[:8])
        chosen.update(c.con_id for c in following[:4])
    return tuple(sorted(i for i in chosen if i in quotes))
