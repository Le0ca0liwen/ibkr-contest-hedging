from .config import NY


def screen_snapshot(manifest, quotes, snapshot):
    """Retain filled inventory AND working legs, including orders closing to zero."""
    retained = snapshot.quantities()
    for order in snapshot.orders:
        if order.signed_remaining:
            retained.setdefault(order.con_id, order.signed_remaining)
    return screen_options(manifest, quotes, retained, snapshot.at.astimezone(NY).date())


def neighbouring_expiries(expiries, primary):
    """Other expiries nearest `primary`, alternating sides (e.g. SPY Nov 19, Nov 23, Nov 18, ...)."""
    others = sorted(set(expiries) - {primary})
    if primary is None:
        return others
    before = sorted((e for e in others if e < primary), reverse=True)
    after = [e for e in others if e > primary]
    ordered = []
    while before or after:
        # Take the closer side first; on a tie prefer the later expiry.
        if after and (not before or (after[0] - primary) <= (primary - before[0])):
            ordered.append(after.pop(0))
            if before:
                ordered.append(before.pop(0))
        else:
            ordered.append(before.pop(0))
            if after:
                ordered.append(after.pop(0))
    return ordered


def screen_options(manifest, quotes, actual, today=None):
    """Screen by moneyness/expiry deterministically, preserving held hedges. `today` dates the
    short-dated tier (policy.short_dated_candidates); without it the tier is skipped."""
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
        neighbours = neighbouring_expiries({c.expiry for c in options}, primary)

        def nearest(expiry, options=options):
            group = [c for c in options if c.expiry == expiry]
            return sorted(group, key=lambda c: (abs(c.strike / quotes[c.con_id].spot - 1), c.con_id))

        same = nearest(primary)
        chosen.update(c.con_id for c in same[:8])
        # Without same-expiry candidates, the nearest two neighbours take that share.
        for expiry in neighbours[: 1 if same else 2]:
            chosen.update(c.con_id for c in nearest(expiry)[: 4 if same else 6])
        tier = manifest.policy.short_dated_candidates
        if tier and primary is not None and today is not None:
            short = sorted(
                c.expiry
                for c in options
                if c.expiry < primary and (c.expiry - today).days >= manifest.policy.short_dated_min_days
            )
            if short:
                chosen.update(c.con_id for c in nearest(short[0])[:tier])
    return tuple(sorted(i for i in chosen if i in quotes))
