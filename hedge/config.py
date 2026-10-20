from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
import yaml
from pydantic import Field, model_validator

from .models import Contract, Dividend, Frozen, digest

NY = ZoneInfo("America/New_York")


class RiskLimits(Frozen):
    stress_loss: float = Field(default=0.03, gt=0, le=0.03)
    liquidity_buffer: float = Field(default=0.25, ge=0.25, lt=1)
    gross_notional: float = Field(default=3, gt=0, le=3)
    stock_notional: float = Field(default=0.5, gt=0, le=0.5)
    residual_delta: float = Field(default=0.1, gt=0, le=0.2)
    contracts_per_option: int = Field(default=50, gt=0, le=50)
    option_names: int = Field(default=6, gt=0, le=6)
    stock_quote_age: int = Field(default=5, gt=0)
    option_quote_age: int = Field(default=60, gt=0)
    greek_age: int = Field(default=60, gt=0)
    account_age: int = Field(default=180, gt=0)
    rate_stress_shift: float = Field(default=0.01, ge=0.005, le=0.05)


class TradingPolicy(Frozen):
    risk_seconds: int = Field(default=5, ge=1)
    stock_seconds: int = Field(default=60, ge=15)
    option_seconds: int = Field(default=900, ge=60)
    reconcile_seconds: int = Field(default=60, ge=10)
    score_threshold: float = Field(default=100, ge=0)
    uncertainty_multiplier: float = Field(default=1.645, ge=0)
    option_cutoff_minutes: int = Field(default=15, ge=2)
    stock_cutoff_minutes: int = Field(default=2, ge=1)
    solver_seconds: float = Field(default=10, gt=0, le=10)
    ranking_seconds: float = Field(default=1, gt=0, le=5)
    ranking_nodes: int = Field(default=32, ge=1, le=10000)
    commission_share: float = Field(default=0.005, ge=0)
    commission_option: float = Field(default=0.65, ge=0)
    minimum_commission: float = Field(default=1, ge=0)
    slippage_spread_fraction: float = Field(default=0.1, ge=0)
    borrow_rate: float = Field(default=0.05, ge=0)
    financing_rate: float = Field(default=0.06, ge=0)
    cash_rate: float = Field(default=0, ge=0)
    cash_rate_source: str | None = None
    cash_rate_status: Literal["unconfigured", "modeled", "verified"] = "unconfigured"
    cash_interest_free_balance: float = Field(default=10000, ge=0)
    cash_day_count: Literal[360, 365] = 360
    seed: int = 17
    history_sessions: Literal[20, 60, 120] = 60

    @model_validator(mode="after")
    def interest_provenance(self):
        if self.cash_rate and (self.cash_rate_status == "unconfigured" or not self.cash_rate_source):
            raise ValueError("Positive cash-interest forecasts require rate status and provenance")
        return self


class ClosingCapturePolicy(Frozen):
    enabled: bool = True
    window_seconds: int = Field(default=180, ge=15, le=300)
    retry_seconds: int = Field(default=15, ge=5, le=60)


class ExecutionPolicy(Frozen):
    stock_slice: int = Field(default=100, ge=1, le=1000)
    option_slice: int = Field(default=1, ge=1, le=10)
    minimum_stock_notional: float = Field(default=1000, ge=0)
    minimum_slice_improvement: float = Field(default=1, ge=0)
    stock_spread_absolute: float = Field(default=0.20, gt=0)
    stock_spread_relative: float = Field(default=0.002, gt=0, le=0.05)
    option_spread_absolute: float = Field(default=0.50, gt=0)
    option_spread_relative: float = Field(default=0.15, gt=0, le=0.50)
    stock_wait_seconds: int = Field(default=15, ge=5, le=120)
    option_wait_seconds: int = Field(default=60, ge=10, le=300)
    setup_wait_seconds: int = Field(default=120, ge=30, le=600)
    stock_lifetime_seconds: int = Field(default=60, ge=30, le=300)
    option_lifetime_seconds: int = Field(default=180, ge=60, le=900)
    setup_lifetime_seconds: int = Field(default=300, ge=120, le=1800)
    setup_opening_wait_minutes: int = Field(default=5, ge=0, le=60)
    setup_cutoff_minutes: int = Field(default=1, ge=1, le=30)
    urgent_market_stocks: bool = True
    market_quote_age: float = Field(default=1, gt=0, le=2)
    market_spread_absolute: float = Field(default=0.02, gt=0, le=0.05)
    market_depth_multiple: int = Field(default=5, ge=2)
    market_slippage_bps: float = Field(default=5, gt=0, le=20)


class Manifest(Frozen):
    name: str
    beta_benchmark: str = Field(default="SPY", min_length=1)
    account: str
    environment: Literal["paper"] = "paper"
    day_zero: date
    mandatory_purchase_session: date | None = None
    scoring_sessions: tuple[date, ...]
    valuation_convention: str
    report_source: str
    scoring_rule: Literal[
        "raw_nav_absolute", "pending_interest_adjustment", "interest_benchmark", "actual_daily_cash_rate"
    ] = "raw_nav_absolute"
    scoring_annual_rate: Decimal | None = Field(default=None, ge=0, le=1)
    scoring_daily_rates: dict[date, Decimal] = Field(default_factory=dict)
    scoring_forecast_daily_rate: Decimal | None = Field(default=None, ge=0, le=1)
    scoring_forecast_source: str = ""
    market_data_provider: Literal["ibkr", "moomoo"] = "ibkr"
    daily_stock_history: str | None = None
    closing_capture: ClosingCapturePolicy = ClosingCapturePolicy()
    execution: ExecutionPolicy = ExecutionPolicy()
    recovery: dict = Field(default_factory=dict)
    initial_nav: Decimal = Field(default=Decimal("1000000"), gt=0)
    contracts: tuple[Contract, ...]
    contest_rules: Literal["practice", "spcx_rklb_spy"] = "practice"
    contest_option_expiry: date | None = None
    mandatory: dict[int, int]
    allowlist: tuple[int, ...]
    dividends: dict[str, tuple[Dividend, ...]] = Field(default_factory=dict)
    risk_free_rate: float = 0.04
    dividend_yields: dict[str, float] = Field(default_factory=dict)
    risk: RiskLimits = RiskLimits()
    policy: TradingPolicy = TradingPolicy()
    gateway_host: Literal["127.0.0.1"] = "127.0.0.1"
    gateway_port: Literal[4002, 7497] = 4002
    client_id: int = Field(default=71, gt=0)
    confirmed: bool = False
    strategy: Literal["dynamic_l1", "delta", "greek", "min_variance", "historical_l1", "two_close"] = (
        "dynamic_l1"
    )

    @model_validator(mode="after")
    def validate_contest(self):
        if self.recovery:
            from .recovery import RecoveryConfig

            RecoveryConfig.model_validate(self.recovery)
        if any(
            day not in self.scoring_sessions or not rate.is_finite() or not Decimal(0) <= rate <= Decimal(1)
            for day, rate in self.scoring_daily_rates.items()
        ):
            raise ValueError(
                "Daily benchmark rates require scored sessions and finite fractional rates between 0 and 1"
            )
        if self.scoring_rule == "interest_benchmark" and self.scoring_annual_rate is None:
            raise ValueError("Instructor's annual benchmark rate is required")
        if self.confirmed and self.scoring_rule == "pending_interest_adjustment":
            raise ValueError(
                "Instructor's interest-adjusted scoring formula is pending; configuration cannot activate"
            )
        if not self.account.startswith("DU"):
            raise ValueError("Only a configured DU paper account is supported")
        if not self.scoring_sessions or tuple(sorted(set(self.scoring_sessions))) != self.scoring_sessions:
            raise ValueError("Scoring sessions must be nonempty, sorted, and unique")
        if self.day_zero >= self.scoring_sessions[0]:
            raise ValueError("Day 0 must precede the first scored session")
        calendar = xcals.get_calendar("XNYS")
        for day in (self.day_zero, *self.scoring_sessions):
            if not calendar.is_session(pd.Timestamp(day)):
                raise ValueError(f"{day} is not an exchange session")
        if self.purchase_session not in (self.day_zero, *self.scoring_sessions):
            raise ValueError("Mandatory purchase session must be Day 0 or an explicit scored session")
        ids = [c.con_id for c in self.contracts]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate contract IDs")
        if not self.mandatory or any(q == 0 for q in self.mandatory.values()):
            raise ValueError("Mandatory positions must have nonzero fixed quantities")
        if self.contest_rules == "practice" and any(q < 0 for q in self.mandatory.values()):
            raise ValueError("Signed mandates require the spcx_rklb_spy contest rules")
        if not set(self.mandatory).issubset(ids) or not set(self.allowlist).issubset(ids):
            raise ValueError("Every mandate and allowlist ID must have a contract definition")
        if set(self.mandatory) & set(self.allowlist):
            raise ValueError("Mandatory contracts cannot be discretionary hedge instruments")
        if len(set(self.allowlist)) != len(self.allowlist):
            raise ValueError("Duplicate allowlist IDs")
        from .contest_rules import validate_portfolio

        mandate_symbols = {self.contract_map[i].symbol.upper() for i in self.mandatory}
        if {"SPCX", "RKLB", "SPY"}.issubset(mandate_symbols) and self.contest_rules != "spcx_rklb_spy":
            raise ValueError("SPCX/RKLB/SPY contest mandates require explicit contest rules")
        if self.contest_rules == "spcx_rklb_spy":
            validate_portfolio(self)
        for i in self.allowlist:
            if reason := self.prohibited_reason(self.contract_map[i]):
                raise ValueError(f"Forbidden allowlist conId {i}: {reason}")
        for c in self.contracts:
            if c.kind == "OPT":
                if c.expiry < self.scoring_sessions[-1]:
                    raise ValueError(f"{c.label} expires before the final holding session")
                if c.underlying_id not in ids:
                    raise ValueError(f"{c.label} needs its underlying contract")
        if not self.valuation_convention.strip() or not self.report_source.strip():
            raise ValueError("Official valuation convention and report source required")
        return self

    @property
    def contract_map(self):
        return {c.con_id: c for c in self.contracts}

    def prohibited_reason(self, contract):
        from .contest_rules import prohibited_reason

        return prohibited_reason(self, contract)

    @property
    def config_hash(self):
        return digest(self)

    @property
    def contest_id(self):
        # Strategy/risk/allowlist revisions must not erase official score history.
        return digest(
            {
                "name": self.name,
                "account": self.account,
                "day_zero": self.day_zero,
                "sessions": self.scoring_sessions,
                "valuation": self.valuation_convention,
            }
        )

    def session_close(self, day: date) -> datetime:
        return xcals.get_calendar("XNYS").session_close(pd.Timestamp(day)).to_pydatetime()

    def session_open(self, day: date) -> datetime:
        return xcals.get_calendar("XNYS").session_open(pd.Timestamp(day)).to_pydatetime()

    def active_session(self, now: datetime) -> date | None:
        day = now.astimezone(NY).date()
        return day if day in (self.day_zero, *self.scoring_sessions) else None

    def next_close(self, now: datetime) -> datetime | None:
        return next(
            (self.session_close(d) for d in self.scoring_sessions if self.session_close(d) > now), None
        )

    def can_trade(self, now: datetime, kind: str, setup: bool = False) -> bool:
        day = self.active_session(now)
        if day is None:
            return False
        cutoff = (
            self.execution.setup_cutoff_minutes
            if setup
            else self.policy.option_cutoff_minutes
            if kind == "OPT"
            else self.policy.stock_cutoff_minutes
        )
        return self.session_open(day) <= now < self.session_close(day) - timedelta(minutes=cutoff)

    @property
    def purchase_session(self) -> date:
        return self.mandatory_purchase_session or self.day_zero

    def scoring_target(
        self, previous_nav: Decimal, day: date | None = None, daily_rate: Decimal | None = None
    ) -> Decimal:
        if self.scoring_rule == "pending_interest_adjustment":
            raise ValueError("Scoring formula pending")
        if self.scoring_rule == "interest_benchmark":
            return previous_nav * (Decimal(1) + self.scoring_annual_rate / Decimal(360))
        if self.scoring_rule == "actual_daily_cash_rate":
            rate = daily_rate if daily_rate is not None else self.scoring_daily_rates.get(day)
            if rate is None:
                raise ValueError("Actual daily cash benchmark rate missing")
            return previous_nav * (Decimal(1) + rate)
        return previous_nav


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.model_validate(yaml.safe_load(Path(path).read_text()))
