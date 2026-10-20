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
        # Neighbouring expiries on either side of the mandated one (e.g. SPY Nov 19 / Nov 23 when
        # every other Nov 20 SPY option is forbidden), nearest first.
        neighbours = sorted(
            {c.expiry for c in options if c.expiry != primary},
            key=lambda e: (abs((e - primary).days) if primary else 0, e),
        )

        def nearest(expiry, options=options):
            group = [c for c in options if c.expiry == expiry]
            return sorted(group, key=lambda c: (abs(c.strike / quotes[c.con_id].spot - 1), c.con_id))

        same = nearest(primary)
        chosen.update(c.con_id for c in same[:8])
        # Without same-expiry candidates, the nearest two neighbours take that share.
        for expiry in neighbours[: 1 if same else 2]:
            chosen.update(c.con_id for c in nearest(expiry)[: 4 if same else 6])
    return tuple(sorted(i for i in chosen if i in quotes))
