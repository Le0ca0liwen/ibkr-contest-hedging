"""Reference misses per session: (a) mandated book only, never hedged; (b) 09:45 warm-up hedge held to the close.

Close marks: model prices at the official close (relative-scaled), as in sim_replay. Interest accrual during the
session (~$25) is approximated with the app's own cash_carry over the remaining session.
"""
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import LANE, build_manifest, history_file  # noqa: E402
from hedge.cashflows import cash_carry  # noqa: E402
from sim_replay import ReplayMarket  # noqa: E402

runs = LANE / (sys.argv[1] if len(sys.argv) > 1 else "runs")
(LANE / "market").mkdir(parents=True, exist_ok=True)
m = build_manifest(history_file(LANE / "market" / "daily.csv"))
out = {}
for state_file in sorted(runs.glob("warm_*__*/state.json")):
    session = state_file.parent.name.split("__")[1]
    st = json.loads(state_file.read_text())
    mk = ReplayMarket(m, LANE / "market" / f"{session}.npz")
    q0 = mk.quotes(mk.base)
    target = st["target"]
    cash0 = float(m.initial_nav) - sum(q0[i].mid * n * m.contract_map[i].multiplier for i, n in m.mandatory.items())
    nav0 = cash0 + sum(q0[i].mid * n * m.contract_map[i].multiplier for i, n in m.mandatory.items())
    carry_day = cash_carry(m, cash0, 0.0, 6.5 / 24, nav0)
    none = cash0 + carry_day + mk.close_value(dict(m.mandatory)) - target
    pos = {int(k): int(v) for k, v in st["positions"].items()}
    cash = float(Decimal(st["cash"]) + Decimal(st["accrued_cash"]))
    borrow = sum(max(0, -q) * q0[i].spot for i, q in pos.items() if m.contract_map[i].kind == "STK")
    hold = cash + cash_carry(m, cash, borrow, 6.25 / 24, float(st["nav"])) + mk.close_value(pos) - target
    out[session] = {"none": none, "hold_0945": hold, "nav_0945_gap": float(st["nav"]) - target,
                    "warm_hedges": {m.contract_map[i].label: q for i, q in pos.items() if i not in m.mandatory}}
    print(session, f"none {none:+9.0f}  hold09:45 {hold:+9.0f}  gap@09:45 {float(st['nav']) - target:+8.0f}",
          out[session]["warm_hedges"])
(runs / "reference.json").write_text(json.dumps(out, indent=1))
