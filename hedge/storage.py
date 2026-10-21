from __future__ import annotations

import json
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Date, DateTime, Index, Integer, Numeric, String, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .models import Execution, PortfolioSnapshot, utcnow


class Base(DeclarativeBase):
    pass


class Record(Base):
    __tablename__ = "records"
    # Recent-first reads by category (alerts, decisions, transitions, burn cycles) stay range
    # scans as closing-burner records accumulate over the contest.
    __table_args__ = (Index("ix_records_category_account_at", "category", "account", "at"),)
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
# execution_plan keys exposed on intent rows (burn legs are told apart by purpose/cycle_id/leg).
INTENT_METADATA = (
    "order_type",
    "urgency",
    "initial_limit",
    "package_id",
    "net_limit",
    "purpose",
    "cycle_id",
    "leg",
    "attempt",
    "time_in_force",
)
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


def burn_result_id(cycle_id):
    return "result:" + cycle_id


def burn_leg_prefix(cycle_id):
    return cycle_id[:24]


def burn_leg_id(cycle_id):
    """32 hex characters like every intent id (orderRef stays 'hedge:' + 32), cycle-prefixed."""
    if len(cycle_id) < 24 or any(c not in "0123456789abcdef" for c in cycle_id[:24]):
        raise ValueError("Burn cycle ids must be lowercase hex uuids")
    return burn_leg_prefix(cycle_id) + uuid4().hex[:8]


def plan_record_id(intent_id):
    """Deterministic execution_plan record id: per-event metadata lookups by primary key."""
    return "plan:" + intent_id


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
        # create_all never adds indexes to an existing table (e.g. the live PostgreSQL DB).
        for index in Record.__table__.indexes:
            index.create(self.engine, checkfirst=True)

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

    def grouped_records(self, categories, account, *, intent_ids=None, ids=None, since=None):
        """Read related audit categories without one round trip per category.

        Optional filters keep bounded views cheap: `intent_ids` matches payload intent_id and
        `ids` matches record ids.
        """
        grouped = defaultdict(list)
        with self.sessions() as session:
            query = (
                select(Record)
                .where(Record.category.in_(categories), Record.account == account)
                .order_by(Record.at.desc())
            )
            if intent_ids is not None:
                query = query.where(Record.payload["intent_id"].as_string().in_(sorted(intent_ids)))
            if ids is not None:
                query = query.where(Record.id.in_(sorted(ids)))
            if since is not None:
                query = query.where(Record.at >= since)
            rows = session.scalars(query).all()
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

    def intents(self, account, pending_only=False, *, include_metadata=True, limit=None):
        with self.sessions() as session:
            query = select(Intent).where(Intent.account == account).order_by(Intent.created_at.desc())
            if pending_only:
                query = query.where(Intent.state.not_in(TERMINAL))
            if limit is not None:
                query = query.limit(limit)
            rows = session.scalars(query).all()
            if not include_metadata:
                return [{c.name: getattr(r, c.name) for c in Intent.__table__.columns} for r in rows]
            # Pending or recent rows are few: read only their plans, not every historical plan.
            return self._with_metadata(session, account, rows, only_rows=pending_only or limit is not None)

    def plan(self, session, identifier, account):
        """The execution_plan record of one intent (primary key first, legacy scan fallback)."""
        row = session.get(Record, plan_record_id(identifier))
        if row is not None and row.account == account and row.category == "execution_plan":
            return row
        return session.scalars(
            select(Record).where(
                Record.category == "execution_plan",
                Record.account == account,
                Record.payload["intent_id"].as_string() == identifier,
            )
        ).first()

    def intent(self, identifier, account):
        """One intent row with its plan metadata (cheap per-broker-event lookup)."""
        with self.sessions() as session:
            row = session.get(Intent, identifier)
            if row is None or row.account != account:
                return None
            return self._with_metadata(session, account, [row], only_rows=True)[0]

    def intent_by_order(self, order_id, account):
        """Most recent intent (any state) transmitted with this broker order id."""
        with self.sessions() as session:
            row = session.scalars(
                select(Intent)
                .where(Intent.account == account, Intent.broker_order_id == order_id)
                .order_by(Intent.created_at.desc())
                .limit(1)
            ).first()
            return self._with_metadata(session, account, [row], only_rows=True)[0] if row else None

    def _with_metadata(self, session, account, rows, only_rows):
        query = select(Record).where(Record.category == "execution_plan", Record.account == account)
        if only_rows:
            if not rows:
                return []
            wanted = {r.id for r in rows}
            # Plans written since deterministic ids exist are primary-key reads; older plans
            # (random ids) fall back to a JSON-path filter restricted to the remaining intents.
            plans = session.scalars(query.where(Record.id.in_([plan_record_id(i) for i in wanted]))).all()
            missing = wanted - {r.payload.get("intent_id") for r in plans}
            if missing:
                plans += session.scalars(
                    query.where(Record.payload["intent_id"].as_string().in_(sorted(missing)))
                ).all()
        else:
            plans = session.scalars(query).all()
        metadata = {r.payload["intent_id"]: r.payload for r in plans}
        package_ids = {m["package_id"] for m in metadata.values() if m.get("package_id")}
        packages = (
            {
                r.id: r.payload
                for r in session.scalars(
                    select(Record).where(
                        Record.category == "setup_package",
                        Record.account == account,
                        Record.id.in_(package_ids),
                    )
                ).all()
            }
            if package_ids
            else {}
        )
        for meta in list(metadata.values()):
            if meta.get("package_id") in packages:
                meta = {**meta, "net_limit": packages[meta["package_id"]]["limit_price"]}
                metadata[meta["intent_id"]] = meta
        return [
            {
                **{c.name: getattr(r, c.name) for c in Intent.__table__.columns},
                **{k: v for k, v in metadata.get(r.id, {}).items() if k in INTENT_METADATA},
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
            row = self.plan(session, identifier, account)
            return row.payload if row is not None else None

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
            plan = self.plan(session, request.intent_id, request.account)
            if plan is not None:
                plan.payload = {
                    **plan.payload,
                    "order_type": request.order_type,
                    "urgency": request.urgency,
                }
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
            if self.plan(session, identifier, row.account) is None:
                session.add(
                    Record(
                        id=plan_record_id(identifier),
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
                        id=plan_record_id(identifier),
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

    def create_burn_leg(self, request, plan):
        """One transaction: a CREATED burn-leg intent and its execution_plan (purpose='burn')."""
        with self.transaction() as session:
            if session.get(Intent, request.intent_id):
                raise ValueError("Duplicate durable intent")
            if session.get(Record, plan["cycle_id"]) is None:
                raise ValueError("Burn leg requires its durable burn_cycle record")
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
            session.add(
                Record(
                    id=plan_record_id(request.intent_id),
                    category="execution_plan",
                    account=request.account,
                    at=self.now(),
                    payload=payload({**plan, "intent_id": request.intent_id, "purpose": "burn"}),
                )
            )

    def open_burn_cycle_ids(self, account):
        """Burn cycles without a final result record (durable hedge block)."""
        with self.sessions() as session:
            cycles = session.scalars(
                select(Record.id).where(Record.category == "burn_cycle", Record.account == account)
            ).all()
            if not cycles:
                return []
            done = set(
                session.scalars(
                    select(Record.id).where(Record.category == "burn_cycle_result", Record.account == account)
                ).all()
            )
            return [c for c in cycles if burn_result_id(c) not in done]

    def burn_legs(self, account, cycle_id):
        """Intents (with metadata) of one burn cycle, oldest first.

        Burn-leg intent ids start with the cycle id's first 24 hex digits (burn_leg_id), so this
        is a plain id prefix match: no JSON scan and no dependence on contest history or clocks.
        """
        with self.sessions() as session:
            rows = session.scalars(
                select(Intent).where(
                    Intent.account == account, Intent.id.like(burn_leg_prefix(cycle_id) + "%")
                )
            ).all()
            legs = self._with_metadata(session, account, rows, only_rows=True)
            # Entry first, then exits by attempt: deterministic even when timestamps tie.
            return sorted(
                (r for r in legs if r.get("cycle_id") == cycle_id),
                key=lambda r: (r["created_at"], r.get("leg") != "entry", r.get("attempt") or 0, r["id"]),
            )

    def fills_for_refs(self, account, references):
        """Effective fills (with commissions) for the given order references."""
        references = set(references)
        if not references:
            return []
        return [f for f in self.effective_fills(account, references) if f["order_ref"] in references]

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

    def fills(self, account, references=None, limit=None):
        with self.sessions() as session:
            query = (
                select(Fill, Fee)
                .outerjoin(Fee, Fee.id == Fill.id)
                .where(Fill.account == account)
                .order_by(Fill.at.desc())
            )
            if references is not None:
                query = query.where(Fill.order_ref.in_(sorted(references)))
            if limit is not None:
                query = query.limit(limit)
            rows = session.execute(query).all()
            return [
                {
                    **{c.name: getattr(r, c.name) for c in Fill.__table__.columns},
                    "commission": fee.amount if fee else None,
                    "commission_currency": fee.currency if fee else None,
                }
                for r, fee in rows
            ]

    def effective_fills(self, account, references=None):
        rows = self.fills(account, references)
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
