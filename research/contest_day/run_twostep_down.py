"""Run sim.py with two SPY steps: +0.15% at 15:27:30 then -0.05% at 15:28:20."""
import os, runpy, sys
from datetime import datetime
sys.argv = ["sim.py", "--name", "D_spyjump3", "--start", "15:25:00", "--end", "15:31:00", "--burner", "paper",
            "--surplus", "150", "--spy-jump", "15:27:30,0.0015"]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim
from hedge.config import NY
original = sim.Market.spot
second = datetime(2026, 10, 22, 15, 28, 20, tzinfo=NY)

def spot(self, symbol, now):
    value = original(self, symbol, now)
    return value * 0.9995 if symbol == "SPY" and now >= second else value

sim.Market.spot = spot
sim.main()
