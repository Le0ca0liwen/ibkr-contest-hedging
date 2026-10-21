"""GET-only main-app reader and a fictional, deterministic cycle simulator."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import numpy as np

from hedge.config import NY
from hedge.demo import demo_manifest, demo_quotes
from hedge.models import PortfolioSnapshot, Position, digest
from hedge.scenarios import ScenarioEngine

from .models import Input, ScenarioPayload


def scenario_payload(scenarios):
    return ScenarioPayload(
        ids=scenarios.ids,
        changes=scenarios.changes.tolist(),
        weights=scenarios.weights.tolist(),
        stress=scenarios.stress.tolist(),
        calendar_days=scenarios.calendar_days,
        source=scenarios.source,
        identifier=scenarios.identifier,
        event_caution=dict(scenarios.event_caution),
    )


class AppSource:
    def __init__(self, settings, transport=None):
        self.client = httpx.AsyncClient(
            base_url=settings.source_url.rstrip("/"),
            timeout=3,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def fetch(self):
        response = await self.client.get("/api/addons/surplus-input")
        response.raise_for_status()
        return Input.model_validate(response.json())

    async def close(self):
        await self.client.aclose()


class DemoSource:
    def __init__(self, entry_window_minutes=30):
        m = demo_manifest()
        self.manifest = type(m).model_validate(
            {
                **m.model_dump(),
                "name": "Surplus add-on synthetic rehearsal",
                "account": "DU_ADDON_SIM_" + uuid4().hex[:8],
            }
        )
        self.now = m.session_close(m.scoring_sessions[0]) - timedelta(minutes=entry_window_minutes)
        self.start = self.now
        self.loss = Decimal(0)
        self.cycles = 0
        # A declared fictional structural hedge makes a full-window capacity
        # rehearsal meaningful. These are not new positions in any broker account.
        self.holdings = {**self.manifest.mandatory, 103: -13, 104: -12, 105: -13, 106: -12}
        self.start_marks = demo_quotes(self.manifest, self.now)
        self.engine = ScenarioEngine(self.manifest)

    async def fetch(self):
        return self.packet()

    def packet(self):
        m, quotes = self.manifest, demo_quotes(self.manifest, self.now)
        values = sum(
            Decimal(str(quotes[i].mid)) * q * m.contract_map[i].multiplier for i, q in self.holdings.items()
        )
        start_values = sum(
            Decimal(str(self.start_marks[i].mid)) * q * m.contract_map[i].multiplier
            for i, q in self.holdings.items()
        )
        nav = Decimal("1000600") + values - start_values - self.loss
        snapshot = PortfolioSnapshot(
            account=m.account,
            at=self.now,
            account_at=self.now,
            nav=nav,
            cash=nav - values,
            excess_liquidity=Decimal("800000") - self.loss,
            initial_margin=Decimal("200000"),
            positions=tuple(Position(con_id=i, quantity=q) for i, q in self.holdings.items()),
            source="synthetic",
        )
        # No next-session forecast is substituted after today's close.
        scenarios = (
            self.engine.build(snapshot, quotes, self.now)
            if self.now < m.session_close(m.scoring_sessions[0])
            else None
        )
        return Input(
            generated_at=self.now,
            manifest=m,
            snapshot=snapshot,
            quotes=quotes,
            scenarios=scenario_payload(scenarios) if scenarios is not None else None,
            scenario_at=self.now,
            scenario_snapshot_id=snapshot.version,
            scenario_revision=self.cycles,
            broker_revision=self.cycles,
            baseline_nav=Decimal("1000000"),
            nav_estimate=float(nav),
            target=Decimal("1000000"),
            target_estimated=False,
            forecast_close=m.session_close(m.scoring_sessions[0]),
            main_mode="SYNTHETIC",
            execution_busy=False,
            config_hash=m.config_hash,
            snapshot_id=snapshot.version,
        )

    def simulate(self, packet, proposal):
        if packet.snapshot.source != "synthetic" or packet.snapshot.account != self.manifest.account:
            raise ValueError("Synthetic cycles require this rehearsal's fictional account")
        if packet.snapshot_id != self.packet().snapshot_id:
            raise ValueError("Simulation proposal is stale")
        seconds = proposal["cycle_seconds_assumption"]
        scenario = packet.scenarios.array()
        column = scenario.ids.index(proposal["con_id"])
        # Fixed sequence; no cherry-picking favorable outcomes from the scenarios.
        rng = np.random.default_rng(self.manifest.policy.seed + self.cycles)
        index = rng.choice(len(scenario.weights), p=scenario.weights)
        remaining = max(1, (packet.forecast_close - self.now).total_seconds())
        move = scenario.changes[index, column] * np.sqrt(min(1, seconds / remaining))
        buy = Decimal(proposal["buy_cap"])
        # A hypothetical exit requote after holding, not a promised fixed-collar fill.
        sell = Decimal(proposal["sell_floor"]) + Decimal(str(move))
        size = proposal["quantity"]
        q, p = packet.quotes[proposal["con_id"]], self.manifest.policy
        fee = Decimal(
            str(
                max(
                    p.minimum_commission,
                    p.commission_share * min(self.manifest.execution.stock_slice, q.ask_size),
                )
            )
        )
        fee += Decimal(
            str(
                max(
                    p.minimum_commission,
                    p.commission_share * min(self.manifest.execution.stock_slice, q.bid_size),
                )
            )
        )
        loss = (buy - sell) * size + fee
        if sell <= 0 or float(loss) > proposal["expense_reserve"] + 1e-7:
            raise ValueError("Synthetic realization outside the reserved model; no cycle recorded")
        return {
            "id": digest((self.manifest.account, self.cycles, packet.snapshot_id)),
            "session": str(self.now.astimezone(NY).date()),
            "account": self.manifest.account,
            "evidence": "synthetic",
            "con_id": proposal["con_id"],
            "quantity": size,
            "seconds": seconds,
            "loss": str(loss),
            "fees": str(fee),
            "execution_expense": str((buy - Decimal(proposal["sell_floor"])) * size + fee),
            "turnover": str((buy + sell) * size),
            "buy": str(buy),
            "sell": str(sell),
            "at": self.now.isoformat(),
            "snapshot_id": packet.snapshot_id,
            "scenario_id": scenario.identifier,
            "position_restored": True,
            "hypothetical_exit_requoted": True,
            "real_orders": 0,
        }

    def apply(self, cycle):
        self.loss += Decimal(cycle["loss"])
        self.cycles += 1
        self.now += timedelta(seconds=cycle["seconds"])
