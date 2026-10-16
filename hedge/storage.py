from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Date, DateTime, Integer, Numeric, String, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .models import Execution, PortfolioSnapshot, utcnow


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "records"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)


class ClosingNAV(Base):
    __tablename__ = "closing_nav_revisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    contest: Mapped[str] = mapped_column(String(64), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    nav: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    source: Mapped[str] = mapped_column(String(500))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Intent(Base):
    __tablename__ = "order_intents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    con_id: Mapped[int] = mapped_column(Integer, index=True)
    signed_quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    decision_id: Mapped[str] = mapped_column(String(64))
    snapshot_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), index=True)
    broker_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    perm_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Fill(Base):
    __tablename__ = "executions"
    # account scopes exec IDs; corrections remain separate immutable records.
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    exec_id: Mapped[str] = mapped_column(String(100))
    order_id: Mapped[int] = mapped_column(Integer)
    order_ref: Mapped[str] = mapped_column(String(100), index=True)
    con_id: Mapped[int] = mapped_column(Integer)
    signed_quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    correction_of: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Fee(Base):
    __tablename__ = "commission_reports"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    currency: Mapped[str] = mapped_column(String(10))


TERMINAL = {"FILLED", "CANCELLED", "REJECTED", "ABANDONED"}
TRANSITIONS = {
    "CREATED": {"VALIDATED", "REJECTED"},
    "VALIDATED": {"SUBMITTING", "REJECTED"},
    "SUBMITTING": {"ACKNOWLEDGED", "UNKNOWN", "PARTIALLY_FILLED", "FILLED", "REJECTED"},
    "UNKNOWN": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "ABANDONED"},
    "ACKNOWLEDGED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED", "REJECTED", "UNKNOWN"},
    "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCEL_PENDING", "CANCELLED", "UNKNOWN"},
    "CANCEL_PENDING": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "UNKNOWN"},
    "CANCELLED": {"FILLED"},  # a late broker execution wins over a cancellation callback
}


def payload(value):
    return (
        value.model_dump(mode="json")
        if hasattr(value, "model_dump")
        else json.loads(json.dumps(value, default=str))
    )


class Store:
    def __init__(self, url="sqlite:///data/hedge.db"):
        self.clock = utcnow
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
        )
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)

    def now(self):
        return self.clock().astimezone(UTC)

    @contextmanager
    def transaction(self):
        with self.sessions.begin() as session:
            yield session

    def record(self, category, account, value, record_id=None):
        identifier = record_id or uuid4().hex
        with self.transaction() as session:
            if session.get(Record, identifier) is None:
                session.add(
                    Record(
                        id=identifier,
                        category=category,
                        account=account,
                        at=self.now(),
                        payload=payload(value),
                    )
                )
        return identifier

    def records(self, category, account, limit=100):
        with self.sessions() as session:
            rows = session.scalars(
                select(Record)
                .where(Record.category == category, Record.account == account)
                .order_by(Record.at.desc())
                .limit(limit)
            ).all()
            return [{"id": r.id, "at": r.at.isoformat(), **r.payload} for r in rows]

    def snapshot(self, snap: PortfolioSnapshot):
        self.record("snapshot", snap.account, snap, snap.version)

    def grouped_records(self, categories, account):
        """Read related audit categories without one round trip per category."""
        grouped = defaultdict(list)
        with self.sessions() as session:
            rows = session.scalars(
                select(Record)
                .where(Record.category.in_(categories), Record.account == account)
                .order_by(Record.at.desc())
            ).all()
            for row in rows:
                grouped[row.category].append({"id": row.id, "at": row.at.isoformat(), **row.payload})
        return grouped

    def create_intent(self, request):
        with self.transaction() as session:
            if session.get(Intent, request.intent_id):
                raise ValueError("Duplicate durable intent")
            session.add(
                Intent(
                    id=request.intent_id,
                    account=request.account,
                    con_id=request.con_id,
                    signed_quantity=request.signed_quantity,
                    limit_price=request.limit_price,
                    decision_id=request.decision_id,
                    snapshot_id=request.snapshot_id,
                    state="CREATED",
                    created_at=self.now(),
                    updated_at=self.now(),
                )
            )

    def transition(self, identifier, state, order_id=None, perm_id=None):
        with self.transaction() as session:
            row = session.get(Intent, identifier)
            if row is None:
                raise ValueError("Unknown intent")
            if row.state == state:
                return
            if state not in TRANSITIONS.get(row.state, set()):
                raise ValueError(f"Invalid transition {row.state} → {state}")
            changes = {"state": state, "updated_at": self.now()}
            if order_id is not None:
                changes["broker_order_id"] = order_id
            if perm_id:
                changes["perm_id"] = perm_id
            result = session.execute(
                update(Intent).where(Intent.id == identifier, Intent.state == row.state).values(**changes)
            )
            if result.rowcount != 1:
                raise RuntimeError("Concurrent intent mutation")
            session.add(
                Record(
                    id=uuid4().hex,
                    category="order_transition",
                    account=row.account,
                    at=self.now(),
                    payload={"intent_id": identifier, **payload(changes)},
                )
            )

    def intents(self, account, pending_only=False, *, include_metadata=True):
        with self.sessions() as session:
            query = select(Intent).where(Intent.account == account).order_by(Intent.created_at.desc())
            if pending_only:
                query = query.where(Intent.state.not_in(TERMINAL))
            rows = session.scalars(query).all()
            if not include_metadata:
                return [{c.name: getattr(r, c.name) for c in Intent.__table__.columns} for r in rows]
            plans = session.scalars(
                select(Record).where(Record.category == "execution_plan", Record.account == account)
            ).all()
            metadata = {r.payload["intent_id"]: r.payload for r in plans}
            packages = {
                r.id: r.payload
                for r in session.scalars(
                    select(Record).where(Record.category == "setup_package", Record.account == account)
                ).all()
            }
            for meta in metadata.values():
                if meta.get("package_id") in packages:
                    meta = {**meta, "net_limit": packages[meta["package_id"]]["limit_price"]}
                    metadata[meta["intent_id"]] = meta
            return [
                {
                    **{c.name: getattr(r, c.name) for c in Intent.__table__.columns},
                    **{
                        k: v
                        for k, v in metadata.get(r.id, {}).items()
                        if k in ("order_type", "urgency", "initial_limit", "package_id", "net_limit")
                    },
                }
                for r in rows
            ]

    def get_record(self, identifier, account, category):
        with self.sessions() as session:
            row = session.get(Record, identifier)
            if row is None or row.account != account or row.category != category:
                return None
            return row.payload

    def execution_plan(self, identifier, account):
        with self.sessions() as session:
            rows = session.scalars(
                select(Record).where(Record.category == "execution_plan", Record.account == account)
            ).all()
            return next((r.payload for r in rows if r.payload.get("intent_id") == identifier), None)

    def update_package_price(self, identifier, account, price):
        with self.transaction() as session:
            row = session.get(Record, identifier)
            if row is None or row.category != "setup_package" or row.account != account:
                raise ValueError("Unknown protected package")
            row.payload = {**row.payload, "limit_price": str(price)}

    def amend_limit(self, identifier, price, reason):
        with self.transaction() as session:
            row = session.get(Intent, identifier)
            if row is None or row.state not in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                raise ValueError("Only acknowledged orders can amend their limit")
            old = str(row.limit_price)
            row.limit_price = Decimal(str(price))
            row.updated_at = self.now()
            session.add(
                Record(
                    id=uuid4().hex,
                    category="price_amendment",
                    account=row.account,
                    at=self.now(),
                    payload={"intent_id": identifier, "old": old, "new": str(price), **payload(reason)},
                )
            )

    def prepare_request(self, request, policy):
        with self.transaction() as session:
            row = session.get(Intent, request.intent_id)
            if row is None or row.state != "VALIDATED":
                raise ValueError("Only an unsent validated intent may refresh its price")
            row.limit_price = request.limit_price
            row.snapshot_id = request.snapshot_id
            plans = session.scalars(
                select(Record).where(Record.category == "execution_plan", Record.account == request.account)
            ).all()
            for plan in plans:
                if plan.payload.get("intent_id") == request.intent_id:
                    plan.payload = {
                        **plan.payload,
                        "order_type": request.order_type,
                        "urgency": request.urgency,
                    }
                    break
            session.add(
                Record(
                    id=uuid4().hex,
                    category="pretransmission",
                    account=request.account,
                    at=self.now(),
                    payload={
                        "intent_id": request.intent_id,
                        "request": payload(request),
                        "quote": policy["quote"],
                        "scenario_id": policy.get("scenario_id"),
                        "scenario_at": payload(policy.get("scenario_at")),
                    },
                )
            )

    def observe_limit(self, identifier, price):
        """Update displayed price from a broker observation, including old intents."""
        price = Decimal(str(price))
        if not price.is_finite() or price <= 0:
            return
        with self.transaction() as session:
            row = session.get(Intent, identifier)
            if row is None or row.limit_price == price:
                return
            plans = session.scalars(
                select(Record).where(Record.category == "execution_plan", Record.account == row.account)
            ).all()
            if not any(p.payload.get("intent_id") == identifier for p in plans):
                session.add(
                    Record(
                        id=uuid4().hex,
                        category="execution_plan",
                        account=row.account,
                        at=self.now(),
                        payload={
                            "intent_id": identifier,
                            "initial_limit": str(row.limit_price),
                            "order_type": "LMT",
                            "urgency": "LEGACY",
                        },
                    )
                )
            session.add(
                Record(
                    id=uuid4().hex,
                    category="broker_price_observation",
                    account=row.account,
                    at=self.now(),
                    payload={
                        "intent_id": identifier,
                        "old": str(row.limit_price),
                        "confirmed_limit": str(price),
                    },
                )
            )
            row.limit_price = price

    def create_package(self, request, quotes):
        """One transaction persists both legs and their common transmission identity."""
        with self.transaction() as session:
            if session.get(Record, request.package_id):
                raise ValueError("Duplicate package identity")
            a, b = quotes[request.buy_id], quotes[request.sell_id]
            cap = a.mid - b.mid + (a.ask - a.bid + b.ask - b.bid) * 0.6
            session.add(
                Record(
                    id=request.package_id,
                    category="setup_package",
                    account=request.account,
                    at=self.now(),
                    payload={**payload(request), "price_cap": cap},
                )
            )
            for i, sign in [(request.buy_id, 1), (request.sell_id, -1)]:
                identifier = f"{request.package_id}:{i}"
                price = Decimal(str(quotes[i].mid))
                session.add(
                    Intent(
                        id=identifier,
                        account=request.account,
                        con_id=i,
                        signed_quantity=sign * request.quantity,
                        limit_price=price,
                        decision_id=request.decision_id,
                        snapshot_id=request.snapshot_id,
                        state="CREATED",
                        created_at=self.now(),
                        updated_at=self.now(),
                    )
                )
                session.add(
                    Record(
                        id=uuid4().hex,
                        category="execution_plan",
                        account=request.account,
                        at=self.now(),
                        payload={
                            "intent_id": identifier,
                            "order_type": "COMBO_LMT",
                            "urgency": "PATIENT",
                            "initial_limit": str(price),
                            "package_id": request.package_id,
                            "net_limit": str(request.limit_price),
                            "quote": payload(quotes[i]),
                        },
                    )
                )

    def execution(self, execution: Execution):
        key = f"{execution.account}:{execution.exec_id}"
        with self.transaction() as session:
            if session.get(Fill, key):
                return False
            fields = execution.model_dump(exclude={"perm_id"})
            fields["at"] = execution.at.astimezone(UTC)
            session.add(Fill(id=key, **fields))
        return True

    def fee(self, account, exec_id, amount, currency="USD"):
        with self.transaction() as session:
            session.merge(Fee(id=f"{account}:{exec_id}", amount=Decimal(str(amount)), currency=currency))

    def fills(self, account):
        with self.sessions() as session:
            rows = session.execute(
                select(Fill, Fee)
                .outerjoin(Fee, Fee.id == Fill.id)
                .where(Fill.account == account)
                .order_by(Fill.at.desc())
            ).all()
            return [
                {
                    **{c.name: getattr(r, c.name) for c in Fill.__table__.columns},
                    "commission": fee.amount if fee else None,
                    "commission_currency": fee.currency if fee else None,
                }
                for r, fee in rows
            ]

    def effective_fills(self, account):
        rows = self.fills(account)
        corrected_roots = {r["correction_of"].rsplit(".", 1)[0] for r in rows if r["correction_of"]}
        selected = {}
        for row in rows:
            root, _, suffix = row["exec_id"].rpartition(".")
            if root in corrected_roots and suffix.isdigit():
                key, revision = root, int(suffix)
            else:
                key, revision = row["exec_id"], 0
            if key not in selected or revision > selected[key][0]:
                selected[key] = (revision, row)
        return [row for _, row in selected.values()]

    def close(self):
        self.engine.dispose()
