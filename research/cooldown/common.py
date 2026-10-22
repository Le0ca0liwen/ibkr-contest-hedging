"""Shared helpers for the cooldown lane: contest manifest on the real hedge universe, real-data market."""
from __future__ import annotations

import math
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

# research/ root (env RESEARCH_ROOT, default: the folder above this script) and the app source (env REPO,
# default: the repository root above research/; the study itself ran on commit 92b4121).
RESEARCH_ROOT = Path(os.environ.get("RESEARCH_ROOT") or Path(__file__).resolve().parents[1]).resolve()
LANE = RESEARCH_ROOT / "cooldown"
REPO = os.environ.get("REPO") or str(RESEARCH_ROOT.parent)
DATA = RESEARCH_ROOT / "marketdata"
CONFIG = RESEARCH_ROOT / "config" / "small.contest.yaml"
sys.path.insert(0, REPO)
sys.path.insert(0, str(DATA))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from hedge.config import NY, Manifest  # noqa: E402

TODAY_SPOT = {"SPCX": 160.30, "RKLB": 53.24, "SPY": 773.85}
STOCK_SPREAD = {"SPCX": 0.02, "RKLB": 0.02, "SPY": 0.01}
SIM_DAY = date(2026, 10, 22)
# app con_id -> (symbol, expiry yyyymmdd, strike, right) on the REAL hedge universe of the task
OPTIONS = {
    1001: ("SPCX", "20261120", 130.0, "C"), 1002: ("SPCX", "20261120", 145.0, "C"),
    1003: ("SPCX", "20261120", 160.0, "C"), 1004: ("RKLB", "20261120", 45.0, "P"),
    1005: ("RKLB", "20261120", 55.0, "P"), 1006: ("SPY", "20261120", 770.0, "C"),
    1007: ("SPY", "20261120", 770.0, "P"), 1008: ("RKLB", "20261120", 50.0, "P"),
    1009: ("RKLB", "20261127", 50.0, "C"), 1010: ("RKLB", "20261127", 50.0, "P"),
    1011: ("SPCX", "20261120", 145.0, "P"), 1012: ("SPCX", "20261127", 145.0, "C"),
    1013: ("SPCX", "20261127", 145.0, "P"), 1014: ("SPY", "20261113", 770.0, "C"),
    1015: ("SPY", "20261113", 770.0, "P"), 1016: ("SPY", "20261127", 770.0, "C"),
    1017: ("SPY", "20261127", 770.0, "P"),
}


def history_file(path, through="2026-10-20"):
    d = pd.read_csv(DATA / "daily_1y.csv")
    d = d[(d.partial == 0) & (d.date <= through)][["date", "symbol", "close"]]
    d.to_csv(path, index=False)
    return path


def build_manifest(history_path, policy_over=None, execution_over=None, burner_mode="off"):
    data = yaml.safe_load(CONFIG.read_text())
    template = yaml.safe_load(Path(REPO, "config/contest.template.yaml").read_text())
    for c in data["contracts"]:
        if c["con_id"] in OPTIONS:
            sym, exp, k, r = OPTIONS[c["con_id"]]
            assert c["symbol"] == sym and float(c["strike"]) == k and c["right"] == r, c
            c["expiry"] = f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
    data["policy"] = {**data["policy"], "burn_surplus": False, **(policy_over or {})}
    data["execution"] = {**data["execution"], **(execution_over or {})}
    burner = dict(template["burner"])
    burner["mode"] = burner_mode
    data["burner"] = burner
    data["autostart"] = "paper"
    data["daily_stock_history"] = str(history_path)
    return Manifest.model_validate(data)


def option_table():
    c = pd.read_csv(DATA / "options_contracts.csv")
    out = {}
    for cid, (sym, exp, k, r) in OPTIONS.items():
        row = c[(c.symbol == sym) & (c.expiry.astype(str) == exp) & (c.strike == k) & (c.right == r)]
        assert len(row) == 1, (cid, sym, exp, k, r)
        row = row.iloc[0]
        out[cid] = {"iv": float(row.iv_sim), "rel_spread": float(row.rel_spread_sim),
                    "iv_source": row.iv_sim_source, "spread_source": row.rel_spread_sim_source,
                    "today_bid": row.today_bid, "today_ask": row.today_ask}
    return out
