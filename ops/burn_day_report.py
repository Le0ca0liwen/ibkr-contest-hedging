"""Read-only timeline of one session's closing burner: what it believed before each round trip,
what each one cost, and how the account NAV moved meanwhile. Writes nothing.

    .venv/bin/python ops/burn_day_report.py --day 2026-10-22 [--close 20:00] [--database URL]
        [--account DU...]

--close is the scored close the burner worked toward: 20:00 (ET) in the contest from Oct 23,
16:00 for Oct 22 and earlier.
"""

import argparse
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hedge.config import NY  # noqa: E402
from hedge.execution_quality import timestamp  # noqa: E402
from hedge.storage import Store  # noqa: E402


def et(value):
    return timestamp(value).astimezone(NY).strftime("%H:%M:%S") if value else "-"


def money(value):
    return "-" if value is None else f"{float(value):+,.2f}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--database", default=os.environ.get("HEDGE_DATABASE_URL"))
    parser.add_argument("--account")
    parser.add_argument("--close", default="20:00", type=time.fromisoformat, help="Scored close, ET")
    args = parser.parse_args()
    if not args.database:
        sys.exit("Pass --database or set HEDGE_DATABASE_URL")
    store = Store(args.database)
    account = args.account
    if account is None:
        from sqlalchemy import text

        with store.engine.connect() as connection:
            account = connection.execute(
                text("SELECT account FROM records WHERE category='burn_cycle' ORDER BY at DESC LIMIT 1")
            ).scalar()
    close = datetime.combine(args.day, args.close, NY)
    start, end = close - timedelta(minutes=25), close + timedelta(minutes=26)

    def window(category, limit=20000):
        rows = store.records(category, account, limit)
        return sorted(
            (r for r in rows if start <= timestamp(r["at"]) <= end), key=lambda r: timestamp(r["at"])
        )

    cycles = sorted(
        (r for r in store.records("burn_cycle", account, 5000) if r.get("session") == str(args.day)),
        key=lambda r: r["planned_at"],
    )
    results = {
        r["cycle_id"]: r
        for r in store.records("burn_cycle_result", account, 5000)
        if r.get("session") == str(args.day)
    }
    snapshots = window("snapshot")
    nav_updates = []
    for row in snapshots:
        if not nav_updates or nav_updates[-1][0] != row.get("account_at"):
            nav_updates.append((row.get("account_at"), row.get("nav")))

    print(f"Account {account}; {len(cycles)} burn cycles on {args.day}")
    print("\nIBKR NetLiquidation updates seen by the app (account_at ET, nav):")
    for at, nav in nav_updates:
        print(f"  {et(at)}  {float(nav):,.2f}")

    print(
        "\nplanned  | label:qty            | nav_est      | target       | median  | p05     | p95     "
        "| needed  | exp_loss | real_loss | fees  | spent_before | est-target+spent | ibkr_nav_at"
    )
    spent = 0.0
    for c in cycles:
        d = c.get("decision") or {}
        gap = d.get("gap") or {}
        r = results.get(c["cycle_id"], {})
        anchor = max(
            (u for u in nav_updates if u[0] and timestamp(u[0]) <= timestamp(c["planned_at"])),
            default=None,
            key=lambda u: timestamp(u[0]),
        )
        burn_free = (
            float(d["nav_estimate"]) - float(d["target"]) + spent
            if d.get("nav_estimate") is not None and d.get("target") is not None
            else None
        )
        print(
            f"{et(c['planned_at'])} | {c['label'][:14]:14}:{c['quantity']:<4} | {float(d.get('nav_estimate') or 0):12,.2f} "
            f"| {float(d.get('target') or 0):12,.2f} | {money(gap.get('median')):>7} | {money(gap.get('p05')):>7} "
            f"| {money(gap.get('p95')):>7} | {money(d.get('needed')):>7} | {money(c.get('expected_loss')):>8} "
            f"| {money(r.get('realized_loss')):>9} | {money(r.get('fees')):>5} | {spent:12,.2f} "
            f"| {money(burn_free):>16} | {et(anchor[0]) if anchor else '-'}"
        )
        spent += float(r.get("realized_loss") or 0)
    print(f"\nTotal realized burn: {spent:,.2f}")

    print("\nBurner decisions (state, needed, budget, spent, NAV source, reason):")
    for row in window("burn_decision"):
        print(
            f"  {et(row['at'])}  {row.get('state', ''):12} {money(row.get('needed')):>9} "
            f"{money(row.get('budget')):>9} {money(row.get('spent')):>8} {str(row.get('nav_source') or '-')[:22]:22} "
            f"{row.get('reason', '')[:100]}"
        )

    print("\nData waits and resumes (continuity):")
    for row in window("continuity"):
        print(f"  {et(row['at'])}  {row.get('status', ''):13} {str(row.get('reason', ''))[:150]}")

    print("\nAlerts:")
    for row in window("alert"):
        print(f"  {et(row['at'])}  {row.get('severity', ''):7} {row.get('message', '')[:160]}")

    print("\nClosing captures:")
    for row in store.records("closing_capture", account, 50):
        if row.get("date") == str(args.day):
            print(f"  {et(row['at'])}  {row.get('status')} {row.get('phase', '')} nav {row.get('nav')}")


if __name__ == "__main__":
    main()
