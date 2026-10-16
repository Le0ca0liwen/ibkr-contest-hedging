from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def digest(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Contract(Frozen):
    con_id: int = Field(gt=0)
    symbol: str
    kind: Literal["STK", "OPT"]
    currency: str = "USD"
    exchange: str = "SMART"
    multiplier: int = Field(default=1, gt=0)
    expiry: date | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    trading_class: str = ""
    underlying_id: int | None = None

    @model_validator(mode="after")
    def valid_option(self):
        if self.kind == "OPT" and (not self.expiry or not self.right or not self.strike):
            raise ValueError("Options require expiry, strike, and right")
        if self.kind == "STK" and (self.multiplier != 1 or self.expiry is not None):
            raise ValueError("Stock requires multiplier 1 and no expiry")
        if self.currency != "USD":
            raise ValueError("v1 execution supports USD instruments only")
        return self

    @property
    def label(self) -> str:
        if self.kind == "STK":
            return self.symbol
        return f"{self.symbol} {self.expiry} {self.strike:g}{self.right}"


class Dividend(Frozen):
    ex_date: date
    amount: float = Field(ge=0)


class Quote(Frozen):
    con_id: int
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    bid_size: int = Field(ge=0)
    ask_size: int = Field(ge=0)
    observed_at: datetime
    data_type: Literal["live", "delayed", "frozen", "synthetic"] = "live"
    provider: Literal["ibkr", "moomoo", "synthetic", "cboe_datashop", "unknown"] = "unknown"
    spot: float = Field(gt=0)
    iv: float | None = Field(default=None, gt=0)
    delta: float = 1
    gamma: float = 0
    vega: float = 0  # dollars per share per 1.0 volatility change
    theta: float = 0  # dollars per share per calendar day
    rho: float | None = None  # modeled dollars per share per 1.0 interest-rate change
    greek_at: datetime | None = None
    model_price: float | None = None
    shortable_shares: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_quote(self):
        if self.ask < self.bid:
            raise ValueError("Crossed quote")
        if self.observed_at.tzinfo is None:
            raise ValueError("Quote timestamp must be timezone aware")
        return self

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


class Position(Frozen):
    con_id: int
    quantity: int
    average_cost: Decimal = Decimal(0)


class WorkingOrder(Frozen):
    order_id: int
    perm_id: int = 0
    con_id: int
    signed_remaining: int
    order_ref: str = ""
    status: str = "Submitted"


class PortfolioSnapshot(Frozen):
    account: str
    at: datetime
    account_at: datetime
    nav: Decimal
    cash: Decimal
    accrued_cash: Decimal | None = None  # Already included in broker NAV; never add again.
    settled_cash: Decimal | None = None
    excess_liquidity: Decimal
    initial_margin: Decimal
    positions: tuple[Position, ...] = ()
    orders: tuple[WorkingOrder, ...] = ()
    connected: bool = True
    reconciled: bool = True
    source: Literal["ibkr", "synthetic"] = "ibkr"

    def quantities(self) -> dict[int, int]:
        return {p.con_id: p.quantity for p in self.positions if p.quantity}

    def projected(self) -> dict[int, int]:
        result = self.quantities()
        for order in self.orders:
            result[order.con_id] = result.get(order.con_id, 0) + order.signed_remaining
        return result

    @property
    def version(self) -> str:
        return digest(self)


class Target(Frozen):
    quantities: dict[int, int]
    expected_score: float
    current_score: float
    uncertainty: float
    status: str
    snapshot_id: str
    scenario_id: str
    config_hash: str
    estimated_cost: float = 0
    binding: tuple[str, ...] = ()
    explanation: str = ""
    solver_metrics: dict[str, Any] = Field(default_factory=dict)


class RiskResult(Frozen):
    passed: bool
    reasons: tuple[str, ...]
    metrics: dict[str, float]


class OrderRequest(Frozen):
    intent_id: str
    con_id: int
    signed_quantity: int
    limit_price: Decimal = Field(gt=0)
    account: str
    snapshot_id: str
    decision_id: str
    order_type: Literal["LMT", "MKT"] = "LMT"
    urgency: Literal["PATIENT", "NORMAL", "DEADLINE", "CORRECTIVE"] = "NORMAL"


class SetupPackageRequest(Frozen):
    package_id: str
    buy_id: int
    sell_id: int
    quantity: int = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    account: str
    snapshot_id: str
    decision_id: str


class Execution(Frozen):
    exec_id: str
    order_id: int
    perm_id: int = 0
    order_ref: str = ""
    account: str
    con_id: int
    signed_quantity: int
    price: Decimal
    at: datetime
    correction_of: str | None = None
