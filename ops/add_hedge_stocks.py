"""Add US stocks/ETFs (e.g. VOO, IVV) to the contest configuration as hedge instruments.

Qualifies each symbol with IBKR read-only (TWS must be running; nothing is traded), adds it to the
contracts and the allowlist, sets its short-borrow allowance, validates, and rewrites the
configuration after a timestamped backup. The configuration hash changes, so press "Confirm
contest configuration" after restarting the app.

    .venv/bin/python ops/add_hedge_stocks.py --symbols VOO IVV --borrow 300 \\
        --borrow-source "IBKR paper: VOO/IVV easy to borrow (operator, 2026-10-22)"
"""

import argparse
import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def add(manifest, qualified, borrow, borrow_source):
    """The qualified manifest with each new symbol's borrow allowance; validated."""
    added = sorted({c.symbol for c in qualified.contracts} - {c.symbol for c in manifest.contracts})
    data = qualified.model_dump()
    if borrow:
        data["stock_borrow"] = {**qualified.stock_borrow, **{s: borrow for s in added}}
        data["stock_borrow_source"] = "; ".join(
            filter(None, [qualified.stock_borrow_source.strip(), borrow_source.strip()])
        )
    return type(manifest).model_validate(data), added


def main():
    import yaml

    from hedge.config import load_manifest
    from hedge.universe_discovery import qualify_stock_universe

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/local.contest.yaml")
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--borrow", type=int, default=0, help="Shares each new symbol may be short")
    parser.add_argument("--borrow-source", default="", help="Who/what confirmed the borrow allowance")
    args = parser.parse_args()
    if args.borrow and not args.borrow_source.strip():
        sys.exit("--borrow needs --borrow-source (the configuration requires a stated source)")
    path = Path(args.config)
    manifest = load_manifest(path)
    qualified = asyncio.run(qualify_stock_universe(manifest, args.symbols))
    updated, added = add(manifest, qualified, args.borrow, args.borrow_source)
    if not added:
        sys.exit("Nothing to add: every symbol is already in the configuration")
    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(path, backup)
    path.write_text(yaml.safe_dump(updated.model_dump(mode="json"), sort_keys=False))
    check = load_manifest(path)
    for symbol in added:
        contract = next(c for c in check.contracts if c.symbol == symbol and c.kind == "STK")
        tradable = contract.con_id in check.allowlist
        print(
            f"Added {symbol} (conId {contract.con_id}); tradable: {'yes' if tradable else 'NO'}; "
            f"short allowance {check.stock_borrow.get(symbol, 0)} shares"
        )
    print(f"Backup: {backup}")
    print(
        f"New configuration hash {check.config_hash[:8]}: restart the app and press Confirm contest configuration"
    )


if __name__ == "__main__":
    main()
