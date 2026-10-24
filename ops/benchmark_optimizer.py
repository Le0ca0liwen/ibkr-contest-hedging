"""Offline synthetic runtime comparison; never connects to a broker or database.

Run from the repository: .venv/bin/python ops/benchmark_optimizer.py
Use --config config/local.practice.yaml to size the synthetic universe like the
configured account. --baseline compares the tracked HEAD optimizer, read-only.
Output contains no account IDs, credentials, positions or broker quotes.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import types
import warnings
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hedge.config import NY, load_manifest  # noqa: E402
from hedge.demo import demo_manifest, demo_quotes  # noqa: E402
from hedge.models import PortfolioSnapshot, Position  # noqa: E402
from hedge.optimizer import HedgeOptimizer  # noqa: E402
from hedge.scenarios import ScenarioEngine  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument(
        "--baseline-ref", default="HEAD", help="Git revision to compare when --baseline is set"
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--gap", type=float, default=500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be between one and ten")
    m = load_manifest(args.config) if args.config else demo_manifest()
    m = m.model_copy(update={"account": "DU_BENCHMARK"})
    now = datetime.combine(m.scoring_sessions[0], datetime.min.time(), NY).replace(hour=15, minute=40)
    quotes = {i: q.model_copy(update={"data_type": "live"}) for i, q in demo_quotes(m, now).items()}
    nav = float(m.initial_nav) + args.gap
    mark = sum(quotes[i].mid * q * m.contract_map[i].multiplier for i, q in m.mandatory.items())
    snapshot = PortfolioSnapshot(
        account=m.account,
        at=now,
        account_at=now,
        nav=Decimal(str(nav)),
        cash=Decimal(str(nav - mark)),
        excess_liquidity=Decimal(str(0.8 * nav)),
        initial_margin=Decimal(str(0.2 * nav)),
        positions=tuple(Position(con_id=i, quantity=q) for i, q in m.mandatory.items()),
    )
    scenarios = ScenarioEngine(m).build(snapshot, quotes, now)
    classes = [("working_tree", HedgeOptimizer)]
    if args.baseline:
        source = subprocess.run(
            ["git", "show", f"{args.baseline_ref}:hedge/optimizer.py"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        module = types.ModuleType("hedge._benchmark_baseline")
        exec(compile(source, f"{args.baseline_ref}:hedge/optimizer.py", "exec"), module.__dict__)
        classes.insert(0, (f"revision:{args.baseline_ref}", module.HedgeOptimizer))
    report = {
        "evidence": "synthetic computation only; no broker/database; not contest-performance evidence",
        "contracts": len(m.contracts),
        "options": sum(c.kind == "OPT" for c in m.contracts),
        "scenario_rows": len(scenarios.weights),
        "stress_rows": len(scenarios.stress),
        "nav_gap": args.gap,
        "results": [],
    }
    for version, cls in classes:
        for stock_only in (True, False):
            samples = []
            optimizer = cls(m)
            for _ in range(args.repeats):
                start = perf_counter()
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Solution may be inaccurate")
                    target = optimizer.solve(snapshot, quotes, scenarios, m.initial_nav, nav, stock_only)
                sample = {
                    "total_ms": round((perf_counter() - start) * 1000, 3),
                    "status": target.status,
                    "score": target.expected_score,
                    "solver_metrics": target.solver_metrics,
                }
                samples.append(sample)
                print(json.dumps({"version": version, "stock_only": stock_only, **sample}), flush=True)
            report["results"].append(
                {
                    "version": version,
                    "stock_only": stock_only,
                    "samples": samples,
                    "median_ms": statistics.median(s["total_ms"] for s in samples),
                }
            )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
