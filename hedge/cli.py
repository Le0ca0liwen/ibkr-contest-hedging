from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import pandas as pd
import uvicorn

from .config import load_manifest
from .data import import_cboe
from .demo import demo_manifest, rehearsal_manifest
from .research import ReplayEngine, Variant, walk_forward
from .scoring import ScoreLedger
from .service import TradingService
from .storage import Store
from .webapp import create_app


def main():
    parser = argparse.ArgumentParser(description="Options contest hedge: paper-only control and research")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate dates, paper account, and fixed mandates")
    validate.add_argument("config")
    serve = commands.add_parser("serve", help="Launch the localhost dashboard; execution starts paused")
    serve.add_argument("--config")
    serve.add_argument("--demo", action="store_true", help="Synthetic simulator; never connects to IBKR")
    serve.add_argument(
        "--rehearsal", action="store_true", help="Synthetic empty-account Day 0; no IBKR connection"
    )
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--database", default=None)
    serve.add_argument("--history", help="Normalized option quote Parquet for scenario estimation")
    migrate = commands.add_parser("init-db")
    migrate.add_argument("--database", default=None)
    score = commands.add_parser("import-closes")
    score.add_argument("--config", required=True)
    score.add_argument("csv")
    score.add_argument("--database", default=None)
    archive = commands.add_parser("import-cboe")
    archive.add_argument("csv")
    archive.add_argument("output")
    research = commands.add_parser("backtest")
    research.add_argument("--config", required=True)
    research.add_argument("--quotes", required=True)
    research.add_argument(
        "--strategy",
        default="dynamic_l1",
        choices=["dynamic_l1", "delta", "greek", "min_variance", "historical_l1", "two_close"],
    )
    research.add_argument("--stock-minutes", type=int, default=1)
    research.add_argument("--option-minutes", type=int, default=15)
    research.add_argument("--threshold", type=float, default=100)
    research.add_argument(
        "--policy", choices=["threshold", "periodic", "daily", "static", "unhedged"], default="threshold"
    )
    research.add_argument("--spread-multiplier", type=float, default=1)
    research.add_argument("--output", default="reports/backtest.json")
    research.add_argument(
        "--recovery",
        action="store_true",
        help="Evaluate frozen recovery model in quote replay; never promotes automatically",
    )
    sweep = commands.add_parser("walk-forward")
    sweep.add_argument("--config", required=True)
    sweep.add_argument("--quotes", required=True)
    sweep.add_argument(
        "--reference-spot", action="append", required=True, help="SYMBOL=spot at original mandate selection"
    )
    sweep.add_argument("--output", default="reports/walk-forward.json")
    history = commands.add_parser("fetch-history", help="Read-only IBKR historical request")
    history.add_argument("--config", required=True)
    history.add_argument("--con-id", type=int, required=True)
    history.add_argument("--duration", default="20 D")
    history.add_argument("--output", required=True)
    prepare = commands.add_parser(
        "prepare-practice", help="Read-only IBKR contract discovery from confirmed rules"
    )
    prepare.add_argument("--inputs", default="config/local.practice.inputs.yaml")
    prepare.add_argument("--output", default="config/local.practice.yaml")
    probe = commands.add_parser("probe-ibkr", help="Read-only paper account/position check; no orders")
    probe.add_argument("--port", type=int, choices=[7497, 4002], default=7497)
    probe.add_argument("--account")
    probe.add_argument("--output", default="reports/practice-connection.json")
    intake = commands.add_parser(
        "practice-inputs", help="Ask for Day 0, final day and expiry for a new round"
    )
    intake.add_argument("--day-zero")
    intake.add_argument("--end-date")
    intake.add_argument("--expiry")
    intake.add_argument("--purchase-on-day", type=int, choices=[0, 1], default=0)
    intake.add_argument("--output", default="config/local.practice.inputs.yaml")
    expand = commands.add_parser("expand-universe", help="Read-only qualify US stocks/ETFs and options")
    expand.add_argument("--config", required=True)
    expand.add_argument("--symbols", nargs="*", default=[])
    expand.add_argument("--option-symbols", nargs="*", default=[])
    expand.add_argument("--expiries", type=int, default=2, help="Option expiries per symbol")
    expand.add_argument("--strikes", type=int, default=6, help="Strikes nearest the reference per expiry")
    expand.add_argument("--center", nargs="*", default=[], help="SYMBOL=PRICE strike reference")
    expand.add_argument("--output", required=True)
    rec_train = commands.add_parser("train-recovery", help="Freeze deterministic recovery forecasts")
    rec_train.add_argument("--config", required=True)
    rec_train.add_argument("--bars", required=True)
    rec_train.add_argument("--before", required=True)
    rec_train.add_argument("--output", default="data/recovery/model.json")
    rec_research = commands.add_parser(
        "validate-recovery", help="Chronological bar-based forecast research; no promotion"
    )
    rec_research.add_argument("--config", required=True)
    rec_research.add_argument("--bars", required=True)
    rec_research.add_argument("--train-sessions", type=int, choices=[20, 60, 120], default=60)
    rec_research.add_argument("--test-sessions", type=int, default=5)
    rec_research.add_argument("--output", default="reports/recovery/forecast-validation.json")
    args = parser.parse_args()
    try:
        if args.command in ("train-recovery", "validate-recovery"):
            from datetime import datetime

            from .recovery import RecoveryConfig, fit_models, normalize_bars
            from .recovery_research import walk_forward_forecasts

            m = load_manifest(args.config)
            config = RecoveryConfig.model_validate(m.recovery)
            bars = pd.read_parquet(args.bars) if args.bars.endswith(".parquet") else pd.read_csv(args.bars)
            if args.command == "train-recovery":
                before = datetime.fromisoformat(args.before)
                if before.tzinfo is None:
                    raise ValueError("Timezone-aware training cutoff required")
                result = fit_models(bars, m, config, before)
                Path(config.bars_path).parent.mkdir(parents=True, exist_ok=True)
                normalize_bars(bars, before).drop(columns="day").to_parquet(config.bars_path, index=False)
            else:
                result = walk_forward_forecasts(bars, m, config, args.train_sessions, args.test_sessions)
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(result, indent=2))
            print("Recovery artifact saved; bar validation does not authorize paper recovery")
        elif args.command == "expand-universe":
            import yaml

            from .universe_discovery import qualify_stock_universe

            centers = {k: float(v) for k, v in (item.split("=", 1) for item in args.center)}
            result = asyncio.run(
                qualify_stock_universe(
                    load_manifest(args.config),
                    args.symbols,
                    option_symbols=args.option_symbols,
                    expiries=args.expiries,
                    strikes=args.strikes,
                    centers=centers,
                )
            )
            Path(args.output).write_text(yaml.safe_dump(result.model_dump(mode="json"), sort_keys=False))
            print("Qualified universe saved; enable only after joint-history and quote validation")
        elif args.command == "validate":
            manifest = load_manifest(args.config)
            print(
                json.dumps(
                    {
                        "valid": True,
                        "confirmed": manifest.confirmed,
                        "hash": manifest.config_hash,
                        "activation": "Requires broker qualification, fresh risk checks, and official previous close",
                    },
                    indent=2,
                )
            )
        elif args.command == "init-db":
            store = Store(args.database or os.environ.get("HEDGE_DATABASE_URL", "sqlite:///data/hedge.db"))
            store.initialize()
            print("Database initialized; production upgrades use alembic upgrade head")
        elif args.command == "serve":
            if sum([args.demo, args.rehearsal, bool(args.config)]) != 1:
                parser.error("Choose exactly one of --demo, --rehearsal, or --config")
            synthetic = args.demo or args.rehearsal
            manifest = (
                rehearsal_manifest()
                if args.rehearsal
                else demo_manifest()
                if args.demo
                else load_manifest(args.config)
            )
            database = args.database or os.environ.get(
                "HEDGE_DATABASE_URL",
                f"sqlite:///data/rehearsal-{uuid4().hex[:8]}.db"
                if args.rehearsal
                else "sqlite:///data/demo.db"
                if args.demo
                else "sqlite:///data/shadow.db",
            )
            if args.rehearsal and (args.database or os.environ.get("HEDGE_DATABASE_URL")):
                parser.error("Rehearsal uses a fresh isolated synthetic database; unset HEDGE_DATABASE_URL")
            store = Store(database)
            service = TradingService(
                manifest,
                store,
                synthetic,
                pd.read_parquet(args.history) if args.history else None,
                rehearsal=args.rehearsal,
            )
            uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, log_level="info")
        elif args.command == "import-closes":
            manifest = load_manifest(args.config)
            store = Store(args.database or os.environ.get("HEDGE_DATABASE_URL", "sqlite:///data/shadow.db"))
            store.initialize()
            print(json.dumps(ScoreLedger(store, manifest).import_csv(args.csv), indent=2))
        elif args.command == "import-cboe":
            print(f"Imported {import_cboe(args.csv, args.output)} historical quote rows")
        elif args.command in ("backtest", "walk-forward"):
            manifest, frame = load_manifest(args.config), pd.read_parquet(args.quotes)
            if args.command == "backtest":
                variant = Variant(
                    args.strategy, args.stock_minutes, args.option_minutes, args.threshold, policy=args.policy
                )
                recovery = None
                if args.recovery:
                    from .recovery import RecoveryController

                    research_store = Store("sqlite://")
                    research_store.initialize()
                    recovery = RecoveryController(manifest, research_store)
                    recovery.mode = "shadow"  # replay is never an execution activation
                try:
                    result = ReplayEngine(manifest, variant, args.spread_multiplier, recovery).run(frame)
                finally:
                    if recovery:
                        recovery.store.close()
            else:
                reference = {
                    item.split("=", 1)[0]: float(item.split("=", 1)[1]) for item in args.reference_spot
                }
                variants = [
                    Variant(strategy, frequency, max(15, frequency), threshold, window)
                    for strategy in (
                        "dynamic_l1",
                        "delta",
                        "greek",
                        "min_variance",
                        "historical_l1",
                        "two_close",
                    )
                    for frequency in (1, 5, 15, 30, 60, 390)
                    for threshold in (25, 100, 250)
                    for window in (20, 60, 120)
                ]
                variants += [Variant(policy=p) for p in ("static", "daily", "unhedged")]
                result = walk_forward(manifest, frame, reference, variants)
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
        elif args.command == "fetch-history":
            from .broker import IBKRBroker

            async def fetch():
                broker = IBKRBroker(load_manifest(args.config), asyncio.Queue())
                try:
                    await broker.connect()
                    return await broker.history(args.con_id, duration=args.duration)
                finally:
                    await broker.disconnect()

            rows = asyncio.run(fetch())
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_parquet(args.output, index=False)
            print(f"Recorded {len(rows)} broker historical bars; bars are not bid/ask execution evidence")
        elif args.command == "prepare-practice":
            from .practice import prepare_practice

            manifest = asyncio.run(prepare_practice(args.inputs))
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            import yaml

            with destination.open("x") as file:
                file.write(yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False))
            print(
                f"Read-only discovery complete: {destination}. Review metadata before setting confirmed: true."
            )
        elif args.command == "probe-ibkr":
            from .practice import probe_paper

            result = asyncio.run(probe_paper(args.port, args.account))
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(result, indent=2))
            print(f"Paper account connected read-only. Local report: {destination}; no orders submitted.")
        elif args.command == "practice-inputs":
            import exchange_calendars as xcals
            import yaml

            from .practice import PracticeInputs

            zero = date.fromisoformat(args.day_zero or input("Day 0 (YYYY-MM-DD): ").strip())
            end = date.fromisoformat(args.end_date or input("Final scoring day (YYYY-MM-DD): ").strip())
            expiry = date.fromisoformat(
                args.expiry or input("Mandatory BA option expiry (YYYY-MM-DD): ").strip()
            )
            calendar = xcals.get_calendar("XNYS")
            if not calendar.is_session(pd.Timestamp(end)) or end <= zero:
                raise ValueError("Final scoring day must be an exchange session after Day 0")
            sessions = tuple(
                d.date() for d in calendar.sessions_in_range(str(zero), str(end)) if d.date() > zero
            )
            print("Scored sessions (Day 1 onward): " + ", ".join(map(str, sessions)))
            confirmed = (
                input("Confirm these sessions and expiry against the round rules? [y/N]: ").strip().lower()
                == "y"
            )
            value = PracticeInputs(
                rules_confirmed=True,
                day_zero=zero,
                scoring_sessions=sessions,
                expiry=expiry,
                purchase_on_day=args.purchase_on_day,
                report_source="Organizer-reported IBKR daily closing account NAV",
            )
            data = {**value.model_dump(mode="json"), "rules_confirmed": confirmed}
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x") as file:
                file.write(yaml.safe_dump(data, sort_keys=False))
            print(f"Rule intake saved: {destination}; no IBKR connection or orders.")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Configuration/action blocked: {exc}\n")


if __name__ == "__main__":
    main()
