"""Explicit recoverable input failures; never classify arbitrary exceptions as safe."""


class DataUnavailable(ValueError):
    pass


DATA_REASONS = (
    "Non-live quote:",
    "Stale/future quote:",
    "Missing/stale option Greeks:",
    "Stale account value",
    "Future account value",
    "Future option Greeks:",
    "Stale/future portfolio snapshot",
    "Broker disconnected or unreconciled",
)


def data_only(reasons):
    return bool(reasons) and all(reason.startswith(DATA_REASONS) for reason in reasons)


def quote_coverage(quotes, identifiers):
    missing = set(identifiers) - quotes.keys()
    if missing:
        raise DataUnavailable(f"Quote coverage changed; waiting for conIds {sorted(missing)}")
