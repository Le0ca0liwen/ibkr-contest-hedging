from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol

from ib_async import IB, ComboLeg, ExecutionFilter, LimitOrder, MarketOrder, TagValue
from ib_async import Contract as IBContract

from .continuity import DataUnavailable
from .models import Execution, PortfolioSnapshot, Position, Quote, WorkingOrder, utcnow


class BrokerAdapter(Protocol):
    async def connect(self): ...
    async def reconcile(self) -> PortfolioSnapshot: ...
    async def closing_nav_observation(self) -> dict: ...
    def snapshot(self) -> PortfolioSnapshot: ...
    def quotes(self) -> dict[int, Quote]: ...
    def reserve_order_id(self) -> int: ...
    async def submit(self, request, order_id: int): ...
    async def cancel(self, order_id: int): ...
    async def disconnect(self): ...


def finite(value):
    return value is not None and math.isfinite(float(value))


class IBKRBroker:
    def __init__(self, manifest, event_queue: asyncio.Queue):
        self.manifest, self.events = manifest, event_queue
        self.ib = IB()
        self.ib.RaiseRequestErrors = True
        self.ib.RequestTimeout = 15
        self.ib.TimezoneTWS = "America/New_York"
        self.native = {}
        self.qualification_errors = {}
        self.readonly = True
        self.qualified = False
        self.reconciled = False
        self.reconcile_lock = asyncio.Lock()
        self.account_at = datetime.min.replace(tzinfo=timezone.utc)
        self.quote_times, self.greek_times = {}, {}
        self.ticks_by_contract = {}
        self.market_rules = {}
        self.external_quotes = None
        original_computation = self.ib.wrapper.tickOptionComputation

        def option_computation(req_id, tick_type, *args):
            original_computation(req_id, tick_type, *args)
            ticker = self.ib.wrapper.reqId2Ticker.get(req_id)
            if ticker is not None and tick_type == 13:
                self.greek_times[ticker.contract.conId] = utcnow()

        self.ib.wrapper.tickOptionComputation = option_computation
        self.ib.disconnectedEvent += self._disconnected
        self.ib.accountValueEvent += self._account
        self.ib.pendingTickersEvent += self._ticks
        self.ib.execDetailsEvent += self._execution
        self.ib.commissionReportEvent += self._commission
        self.ib.orderStatusEvent += self._order
        self.ib.positionEvent += lambda p: self.emit(
            "position", {"con_id": p.contract.conId, "quantity": str(p.position)}
        )
        self.ib.errorEvent += lambda req, code, msg, contract: self.emit(
            "broker_error",
            {"order_id": req, "code": code, "message": msg, "con_id": getattr(contract, "conId", None)},
        )

    def emit(self, event, value):
        self.events.put_nowait((event, value))

    def _disconnected(self):
        self.reconciled = False
        self.emit("disconnected", {})

    def _account(self, value):
        if value.account == self.manifest.account and value.tag == "NetLiquidation":
            self.account_at = utcnow()

    def _ticks(self, tickers):
        for ticker in tickers:
            identifier = ticker.contract.conId
            if any(t.tickType in (0, 1, 2, 3) for t in ticker.ticks):
                self.quote_times[identifier] = utcnow()
            # Greeks have no independent timestamp in the high-level API. Mark
            # freshness only when an option computation tick arrives.
            if any(t.tickType in (10, 11, 12, 13) for t in ticker.ticks):
                self.greek_times[identifier] = utcnow()

    def _execution(self, trade, fill):
        e = fill.execution
        if e.acctNumber != self.manifest.account:
            return
        if fill.contract.secType == "BAG":
            self.emit(
                "package_execution",
                {
                    "exec_id": e.execId,
                    "order_ref": e.orderRef,
                    "perm_id": e.permId,
                    "quantity": str(e.shares),
                    "net_price": str(e.price),
                    "at": fill.time.isoformat(),
                },
            )
            return  # Aggregate combo report is not an additional asset/fill.
        if not float(e.shares).is_integer():
            self.emit("unsupported_execution", {"exec_id": e.execId, "shares": str(e.shares)})
            return
        correction = None
        # IBKR corrections increment the final component of an exec ID.
        if "." in e.execId and e.execId.rsplit(".", 1)[1].isdigit() and int(e.execId.rsplit(".", 1)[1]) > 1:
            correction = e.execId.rsplit(".", 1)[0] + ".01"
        at = fill.time if fill.time.tzinfo else fill.time.replace(tzinfo=timezone.utc)
        self.emit(
            "execution",
            Execution(
                exec_id=e.execId,
                order_id=e.orderId,
                perm_id=e.permId,
                order_ref=e.orderRef,
                account=e.acctNumber,
                con_id=fill.contract.conId,
                signed_quantity=int(e.shares) * (1 if e.side == "BOT" else -1),
                price=Decimal(str(e.price)),
                at=at,
                correction_of=correction,
            ),
        )

    def _commission(self, trade, fill, report):
        if fill.execution.acctNumber == self.manifest.account and finite(report.commission):
            if fill.contract.secType == "BAG":
                self.emit(
                    "package_commission",
                    {
                        "exec_id": report.execId,
                        "order_ref": fill.execution.orderRef,
                        "amount": str(report.commission),
                        "currency": report.currency,
                    },
                )
                return  # Retain aggregate evidence without charging leg NAV twice.
            self.emit(
                "commission",
                {"exec_id": report.execId, "amount": str(report.commission), "currency": report.currency},
            )

    def _order(self, trade):
        if trade.order.account == self.manifest.account:
            self.emit(
                "order",
                {
                    "order_id": trade.order.orderId,
                    "perm_id": trade.order.permId,
                    "order_ref": trade.order.orderRef,
                    "status": trade.orderStatus.status,
                    "filled": trade.orderStatus.filled,
                    "remaining": trade.orderStatus.remaining,
                    "order_type": trade.order.orderType,
                    "limit_price": trade.order.lmtPrice,
                },
            )

    async def connect(self):
        # connectAsync performs global account downloads too; a manual reconcile
        # must not overwrite their completion futures during startup.
        async with self.reconcile_lock:
            self.qualified = self.reconciled = False
            self.native.clear()
            self.qualification_errors.clear()
            try:
                await self._connect_and_qualify()
            except BaseException:
                self.qualified = self.reconciled = False
                self.ib.disconnect()
                raise

    async def _connect_and_qualify(self):
        await self.ib.connectAsync(
            self.manifest.gateway_host,
            self.manifest.gateway_port,
            clientId=self.manifest.client_id,
            account=self.manifest.account,
            readonly=self.readonly,
            timeout=15,
            raiseSyncErrors=True,
        )
        if self.manifest.account not in self.ib.managedAccounts() or not self.manifest.account.startswith(
            "DU"
        ):
            self.ib.disconnect()
            raise ValueError("Configured paper account not accessible; live trading is disabled")
        for c in self.manifest.contracts:
            try:
                await self._qualify_contract(c)
            except TimeoutError as exc:
                message = f"Contract qualification timed out: {c.label}"
                mandate_ids = set(self.manifest.mandatory)
                mandate_ids |= {
                    self.manifest.contract_map[i].underlying_id
                    for i in self.manifest.mandatory
                    if self.manifest.contract_map[i].kind == "OPT"
                }
                if c.con_id in mandate_ids:
                    raise ConnectionError(message) from exc
                self.qualification_errors[c.con_id] = message
                self.emit("qualification_unavailable", {"con_id": c.con_id, "message": message})
        snapshot = await self._download_state()
        self._validate_required_qualification(snapshot)
        self.qualified = True
        if self.manifest.market_data_provider == "ibkr":
            self.ib.reqMarketDataType(1)
            for native in self.native.values():
                self.ib.reqMktData(native, "236" if native.secType == "STK" else "", False, False)

    def _validate_required_qualification(self, snapshot):
        required = set(self.manifest.mandatory) | set(snapshot.quantities())
        required |= {order.con_id for order in snapshot.orders if order.signed_remaining}
        required |= {
            self.manifest.contract_map[i].underlying_id
            for i in tuple(required)
            if i in self.manifest.contract_map and self.manifest.contract_map[i].kind == "OPT"
        }
        missing = required - self.native.keys()
        foreign = sorted(i for i in missing if i not in self.manifest.contract_map)
        if foreign:
            # Name them the way TWS shows them, so the operator can close them there.
            names = {}
            for position in self.ib.positions(self.manifest.account):
                c = position.contract
                name = (
                    f"{c.symbol} stock"
                    if c.secType == "STK"
                    else f"{c.symbol} {c.lastTradeDateOrContractMonth} {c.strike:g}{c.right}"
                )
                names[c.conId] = f"{name} {position.position:+g}"
            raise ValueError(
                "Positions not in the contest configuration (close them in TWS): "
                + "; ".join(f"{names.get(i, 'conId')} [{i}]" for i in foreign)
            )
        if missing:
            labels = [self.manifest.contract_map[i].label for i in sorted(missing)]
            raise ValueError("Contest contracts not verified by IBKR: " + "; ".join(labels))

    async def _qualify_contract(self, c):
        details = await asyncio.wait_for(
            self.ib.reqContractDetailsAsync(IBContract(conId=c.con_id, exchange=c.exchange)), 15
        )
        if len(details) != 1:
            raise ValueError(f"Ambiguous/unavailable contract ID {c.con_id}")
        native = details[0].contract
        if (
            native.conId != c.con_id
            or native.symbol != c.symbol
            or native.secType != c.kind
            or native.currency != c.currency
        ):
            raise ValueError(f"Contract identity mismatch: {c.con_id}")
        if c.kind == "OPT":
            if (
                native.lastTradeDateOrContractMonth[:8] != c.expiry.strftime("%Y%m%d")
                or native.right != c.right
                or native.strike != c.strike
                or int(native.multiplier) != c.multiplier
            ):
                raise ValueError(f"Option expiry/strike/right/multiplier mismatch: {c.con_id}")
            if c.trading_class and native.tradingClass != c.trading_class:
                raise ValueError(f"Trading class mismatch: {c.con_id}")
        self.ticks_by_contract[c.con_id] = details[0].minTick
        exchanges = details[0].validExchanges.split(",")
        rules = details[0].marketRuleIds.split(",")
        if "SMART" in exchanges and len(rules) == len(exchanges):
            rule_id = int(rules[exchanges.index("SMART")] or 0)
            if rule_id:
                self.market_rules[c.con_id] = await self.ib.reqMarketRuleAsync(rule_id)
        self.native[c.con_id] = native

    def portfolio_marks(self):
        """IBKR's own position marks from account updates (no market-data subscription needed)."""
        marks = {}
        try:
            items = self.ib.portfolio(self.manifest.account)
        except Exception:
            return marks
        for item in items:
            if finite(item.marketPrice):
                marks[item.contract.conId] = {
                    "market_price": float(item.marketPrice),
                    "market_value": float(item.marketValue) if finite(item.marketValue) else None,
                    "position": float(item.position),
                }
        return marks

    async def closing_nav_observation(self):
        """Fresh, explicitly requested account NAV; never reuse summary cache."""
        # Not behind reconcile_lock: the summary has its own request id, so it cannot collide
        # with the keyed reconcile downloads, and the bell window must not wait out a reconnect
        # that re-qualifies every contract.
        if not self.ib.isConnected() or self.manifest.account not in self.ib.managedAccounts():
            raise ConnectionError("Configured paper account unavailable for closing NAV capture")
        observed = []

        def receive(value):
            if (
                value.account == self.manifest.account
                and value.tag == "NetLiquidation"
                and value.currency in ("BASE", "USD")
            ):
                observed.append((Decimal(value.value), utcnow()))

        request_at = utcnow()
        request_id = self.ib.client.getReqId()
        future = self.ib.wrapper.startReq(request_id)
        self.ib.accountSummaryEvent += receive
        try:
            self.ib.client.reqAccountSummary(request_id, "All", "NetLiquidation")
            await asyncio.wait_for(future, 15)
            if not observed or any(not value.is_finite() for value, _ in observed):
                raise ValueError("Fresh IBKR closing NAV unavailable")
            if len({value for value, _ in observed}) != 1:
                raise ValueError("Inconsistent closing NAV observations; retry capture")
            nav, observed_at = observed[-1]
            return {
                "account": self.manifest.account,
                "nav": nav,
                "requested_at": request_at,
                "observed_at": observed_at,
                "source": "ibkr",
            }
        finally:
            self.ib.accountSummaryEvent -= receive
            self.ib.wrapper._endReq(request_id)
            if self.ib.isConnected():
                self.ib.client.cancelAccountSummary(request_id)

    async def reconcile(self):
        # Several IB requests use one global completion future. Overlapping
        # downloads can overwrite it and strand a caller until timeout.
        async with self.reconcile_lock:
            return await self._download_state()

    async def _download_state(self):
        self.reconciled = False
        await self.ib.reqPositionsAsync()
        await self.ib.reqAllOpenOrdersAsync()
        completed = await self.ib.reqCompletedOrdersAsync(False)
        for trade in completed:
            self._order(trade)
        fills = await self.ib.reqExecutionsAsync(ExecutionFilter(acctCode=self.manifest.account))
        for fill in fills:
            self._execution(None, fill)
        # connectAsync already subscribes to account updates. Re-requesting the
        # same subscription can wait indefinitely for a new download-end marker.
        if not self.ib.accountValues(self.manifest.account):
            await asyncio.wait_for(self.ib.reqAccountUpdatesAsync(self.manifest.account), 15)
        self.reconciled = True
        snapshot = self.snapshot()
        try:
            self._validate_required_qualification(snapshot)
        except ValueError:
            self.reconciled = False
            raise
        return snapshot

    def snapshot(self):
        values = {
            v.tag: v.value
            for v in self.ib.accountValues(self.manifest.account)
            if v.currency in ("BASE", "USD", "")
        }
        required = ["NetLiquidation", "TotalCashValue", "ExcessLiquidity", "InitMarginReq"]
        if any(k not in values for k in required):
            raise DataUnavailable("Required account NAV/cash/margin fields unavailable")
        positions = []
        for p in self.ib.positions(self.manifest.account):
            if not float(p.position).is_integer():
                raise ValueError("Fractional holdings require an additional validated adapter")
            positions.append(
                Position(
                    con_id=p.contract.conId, quantity=int(p.position), average_cost=Decimal(str(p.avgCost))
                )
            )
        orders = []
        for t in self.ib.openTrades():
            if t.order.account == self.manifest.account:
                remaining = t.orderStatus.remaining
                if not finite(remaining) or not float(remaining).is_integer():
                    raise ValueError("Unknown/fractional remaining order quantity")
                if t.contract.secType == "BAG":
                    if not t.order.orderRef.startswith("hedge-package:"):
                        raise ValueError("Unknown working combo requires reconciliation")
                    for leg in t.contract.comboLegs:
                        orders.append(
                            WorkingOrder(
                                order_id=t.order.orderId,
                                perm_id=t.order.permId,
                                con_id=leg.conId,
                                signed_remaining=int(remaining)
                                * leg.ratio
                                * (1 if leg.action == "BUY" else -1)
                                * (1 if t.order.action == "BUY" else -1),
                                order_ref=t.order.orderRef,
                                status=t.orderStatus.status,
                            )
                        )
                    continue
                orders.append(
                    WorkingOrder(
                        order_id=t.order.orderId,
                        perm_id=t.order.permId,
                        con_id=t.contract.conId,
                        signed_remaining=int(remaining) * (1 if t.order.action == "BUY" else -1),
                        order_ref=t.order.orderRef,
                        status=t.orderStatus.status,
                    )
                )
        return PortfolioSnapshot(
            account=self.manifest.account,
            at=utcnow(),
            account_at=self.account_at,
            nav=Decimal(values["NetLiquidation"]),
            cash=Decimal(values["TotalCashValue"]),
            accrued_cash=Decimal(values["AccruedCash"]) if "AccruedCash" in values else None,
            settled_cash=Decimal(values["SettledCash"]) if "SettledCash" in values else None,
            excess_liquidity=Decimal(values["ExcessLiquidity"]),
            initial_margin=Decimal(values["InitMarginReq"]),
            positions=tuple(sorted(positions, key=lambda p: p.con_id)),
            orders=tuple(orders),
            connected=self.ib.isConnected(),
            reconciled=self.reconciled,
        )

    def quotes(self):
        if self.external_quotes is not None:
            return {i: q for i, q in self.external_quotes(utcnow()).items() if i in self.native}
        result = {}
        for i, c in self.manifest.contract_map.items():
            native = self.native.get(i)
            if native is None:
                continue
            t = self.ib.ticker(native)
            if t is None or not finite(t.bid) or not finite(t.ask) or t.bid < 0 or t.ask < t.bid:
                continue
            if i not in self.quote_times:
                continue
            g = t.modelGreeks
            spot = (
                t.marketPrice()
                if c.kind == "STK"
                else (g.undPrice if g and finite(g.undPrice) else float("nan"))
            )
            if not finite(spot) or spot <= 0:
                continue
            kwargs = {}
            if c.kind == "OPT":
                if (
                    not g
                    or not all(
                        finite(getattr(g, key)) for key in ("impliedVol", "delta", "gamma", "vega", "theta")
                    )
                    or g.impliedVol <= 0
                ):
                    continue
                # IB vega is per 1 percentage point, internal vega is per unit.
                kwargs = {
                    "iv": g.impliedVol,
                    "delta": g.delta,
                    "gamma": g.gamma,
                    "vega": g.vega * 100,
                    "theta": g.theta,
                    "greek_at": self.greek_times.get(i),
                    "model_price": g.optPrice if finite(g.optPrice) else None,
                }
            result[i] = Quote(
                provider="ibkr",
                con_id=i,
                bid=t.bid,
                ask=t.ask,
                bid_size=max(0, int(t.bidSize)) if finite(t.bidSize) else 0,
                ask_size=max(0, int(t.askSize)) if finite(t.askSize) else 0,
                observed_at=self.quote_times[i],
                spot=spot,
                shortable_shares=max(0, int(t.shortableShares))
                if c.kind == "STK" and finite(t.shortableShares)
                else None,
                data_type={1: "live", 2: "frozen", 3: "delayed", 4: "frozen"}.get(
                    t.marketDataType, "delayed"
                ),
                **kwargs,
            )
        return result

    def reserve_order_id(self):
        return self.ib.client.getReqId()

    def refresh_market_data(self):
        """Operator retry after enabling entitlements; old ticks cannot pass freshness."""
        if not self.ib.isConnected() or not self.qualified:
            raise ValueError("Broker contract qualification required before subscribing")
        self.ib.reqMarketDataType(1)
        for identifier, native in self.native.items():
            if self.ib.ticker(native) is not None:
                self.ib.cancelMktData(native)
            self.quote_times.pop(identifier, None)
            self.greek_times.pop(identifier, None)
            self.ib.reqMktData(native, "236" if native.secType == "STK" else "", False, False)

    def price_tick(self, con_id, price):
        applicable = [r.increment for r in self.market_rules.get(con_id, ()) if price >= r.lowEdge]
        if applicable:
            return applicable[-1]
        return self.ticks_by_contract.get(con_id, 0.01) or 0.01

    async def submit(self, request, order_id):
        if self.readonly or not self.qualified or not self.reconciled or not self.ib.isConnected():
            raise ValueError("Broker is not ready for paper execution")
        if request.con_id not in self.native:
            raise ValueError("Order contract has not passed broker qualification")
        if request.account != self.manifest.account or not request.account.startswith("DU"):
            raise ValueError("Paper account identity rejected")
        if request.con_id not in self.manifest.allowlist:
            raise ValueError("Discretionary trading of mandatory/unapproved instruments is forbidden")
        if request.time_in_force == "IOC" and request.order_type != "LMT":
            raise ValueError("Immediate-or-cancel orders must be marketable limits")
        if request.order_type == "MKT":
            from .order_policy import choose_order

            q = self.quotes().get(request.con_id)
            if (
                q is None
                or request.urgency != "CORRECTIVE"
                or choose_order(
                    self.manifest,
                    self,
                    self.manifest.contract_map[request.con_id],
                    q,
                    request.signed_quantity,
                    utcnow(),
                    corrective=True,
                )["order_type"]
                != "MKT"
            ):
                raise ValueError("Market order urgency/liquidity/freshness rejected")
        order = (MarketOrder if request.order_type == "MKT" else LimitOrder)(
            "BUY" if request.signed_quantity > 0 else "SELL",
            abs(request.signed_quantity),
            *([] if request.order_type == "MKT" else [float(request.limit_price)]),
            orderId=order_id,
            account=request.account,
            orderRef="hedge:" + request.intent_id,
            tif=request.time_in_force,
            outsideRth=False,
        )
        trade = self.ib.placeOrder(self.native[request.con_id], order)
        return {"order_id": trade.order.orderId, "perm_id": trade.order.permId}

    def package_contract(self, request):
        c, h = (self.manifest.contract_map[i] for i in (request.buy_id, request.sell_id))
        if (
            request.buy_id not in self.manifest.mandatory
            or request.sell_id not in self.manifest.allowlist
            or c.kind != "OPT"
            or h.kind != "OPT"
            or c.currency != "USD"
            or h.currency != "USD"
            or c.multiplier != 100
            or h.multiplier != 100
            or c.expiry != h.expiry
            or c.underlying_id != h.underlying_id
            or c.right != h.right
            or c.trading_class != h.trading_class
            or not (h.strike > c.strike if c.right == "C" else h.strike < c.strike)
            or request.buy_id not in self.native
            or request.sell_id not in self.native
        ):
            raise ValueError("Unsupported/unqualified guaranteed protective option pair")
        return IBContract(
            symbol=c.symbol,
            secType="BAG",
            currency="USD",
            exchange="SMART",
            comboLegs=[
                ComboLeg(conId=request.buy_id, ratio=1, action="BUY", exchange="SMART"),
                ComboLeg(conId=request.sell_id, ratio=1, action="SELL", exchange="SMART"),
            ],
        )

    def package_order(self, request, order_id=0):
        return LimitOrder(
            "BUY",
            request.quantity,
            float(request.limit_price),
            orderId=order_id,
            account=request.account,
            orderRef="hedge-package:" + request.package_id,
            tif="DAY",
            outsideRth=False,
            smartComboRoutingParams=[TagValue("NonGuaranteed", "0")],
        )

    async def package_preview(self, request):
        return await self.ib.whatIfOrderAsync(self.package_contract(request), self.package_order(request))

    async def submit_package(self, request, order_id, now):
        if self.readonly or not self.qualified or not self.reconciled or not self.ib.isConnected():
            raise ValueError("Paper broker not ready for protected setup")
        if (
            request.account != self.manifest.account
            or now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
            != self.manifest.purchase_session
        ):
            raise ValueError("Wrong paper account or setup session")
        projected = self.snapshot().projected()
        if projected.get(request.buy_id, 0) + request.quantity > self.manifest.mandatory[request.buy_id]:
            raise ValueError("Protected package exceeds exact mandate")
        trade = self.ib.placeOrder(self.package_contract(request), self.package_order(request, order_id))
        return {"order_id": trade.order.orderId, "perm_id": trade.order.permId}

    async def setup_mandate(self, request, order_id, now):
        raise ValueError("Bare mandatory purchases disabled; use a guaranteed protective package")

    async def cancel(self, order_id):
        for t in self.ib.openTrades():
            if (
                t.order.orderId == order_id
                and t.order.account == self.manifest.account
                and t.order.orderRef.startswith(("hedge:", "hedge-package:"))
            ):
                self.ib.cancelOrder(t.order)
                return
        # Do not claim cancellation succeeded merely because cache has no order.
        raise ValueError("System-owned working order not found; reconcile cancellation")

    async def replace(self, order_id, limit_price):
        for t in self.ib.openTrades():
            if (
                t.order.orderId == order_id
                and t.order.account == self.manifest.account
                and t.order.orderRef.startswith(("hedge:", "hedge-package:"))
            ):
                t.order.lmtPrice = float(limit_price)
                self.ib.placeOrder(t.contract, t.order)
                return
        raise ValueError("Cannot replace absent system order")

    async def margin_preview(self, request):
        order = (MarketOrder if request.order_type == "MKT" else LimitOrder)(
            "BUY" if request.signed_quantity > 0 else "SELL",
            abs(request.signed_quantity),
            *([] if request.order_type == "MKT" else [float(request.limit_price)]),
            account=request.account,
            tif="DAY",
            outsideRth=False,
        )
        return await self.ib.whatIfOrderAsync(self.native[request.con_id], order)

    async def history(self, con_id, end="", duration="20 D", bar_size="1 min", what="TRADES"):
        rows = await self.ib.reqHistoricalDataAsync(
            self.native[con_id],
            endDateTime=end,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what,
            useRTH=True,
            formatDate=2,
        )
        return [
            {
                "timestamp": b.date,
                "con_id": con_id,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "source": "ibkr",
            }
            for b in rows
        ]

    async def disconnect(self):
        self.reconciled = False
        self.ib.disconnect()


class SimBroker:
    """Explicit synthetic demo/replay. Never opens an IBKR connection."""

    def __init__(self, manifest, queue, now, start_empty=False):
        self.manifest, self.events, self.now = manifest, queue, now
        self.positions = {} if start_empty else dict(manifest.mandatory)
        self.cash = manifest.initial_nav
        self.accrued_cash = Decimal(0)
        self.nav = manifest.initial_nav
        self.connected = False
        self.reconciled = False
        self.qualified = True
        self.readonly = True
        self.orders = {}
        self.package_orders = {}
        self.requests = {}
        self._quotes = {}
        self._counter = 1000
        self._exec_counter = 0
        self.filled = {}
        # Test/rehearsal hook: per-IOC fill caps consumed in submission order (0 = no fill).
        self.ioc_fill_caps = []

    async def connect(self):
        self.connected = self.reconciled = True

    async def reconcile(self):
        self.reconciled = self.connected
        return self.snapshot()

    def portfolio_marks(self):
        return {}

    async def closing_nav_observation(self):
        if not self.connected:
            raise ConnectionError("Simulator disconnected")
        return {
            "account": self.manifest.account,
            "nav": self.nav,
            "requested_at": self.now,
            "observed_at": self.now,
            "source": "synthetic",
        }

    def snapshot(self):
        return PortfolioSnapshot(
            account=self.manifest.account,
            at=self.now,
            account_at=self.now,
            nav=self.nav,
            cash=self.cash,
            accrued_cash=self.accrued_cash,
            settled_cash=self.cash,
            excess_liquidity=self.nav * Decimal(".8"),
            initial_margin=self.nav * Decimal(".2"),
            positions=tuple(Position(con_id=i, quantity=q) for i, q in sorted(self.positions.items()) if q),
            orders=tuple(leg for o in self.orders.values() for leg in (o if isinstance(o, tuple) else (o,))),
            connected=self.connected,
            reconciled=self.reconciled,
            source="synthetic",
        )

    def quotes(self):
        return self._quotes

    def update_quotes(self, quotes, now):
        previous = self._quotes
        if previous:
            from .cashflows import cash_carry

            interval = (now - self.now).total_seconds() / 86400
            borrow = sum(
                max(0, -quantity) * previous[i].spot
                for i, quantity in self.positions.items()
                if self.manifest.contract_map[i].kind == "STK" and i in previous
            )
            accrued = Decimal(
                str(cash_carry(self.manifest, float(self.cash), borrow, interval, float(self.nav)))
            )
            self.accrued_cash += accrued
            self.nav += accrued
            for i, q in quotes.items():
                if i in previous:
                    self.nav += Decimal(
                        str(
                            (q.mid - previous[i].mid)
                            * self.positions.get(i, 0)
                            * self.manifest.contract_map[i].multiplier
                        )
                    )
        self._quotes, self.now = quotes, now

    def reserve_order_id(self):
        self._counter += 1
        return self._counter

    async def submit(self, request, order_id):
        if self.readonly or not self.connected:
            raise ValueError("Synthetic broker is read-only/disconnected")
        if request.time_in_force == "IOC":
            return self._submit_ioc(request, order_id)
        self.requests[order_id] = request
        self.orders[order_id] = WorkingOrder(
            order_id=order_id,
            con_id=request.con_id,
            signed_remaining=request.signed_quantity,
            order_ref="hedge:" + request.intent_id,
        )
        self.events.put_nowait(
            (
                "order",
                {
                    "order_id": order_id,
                    "order_ref": "hedge:" + request.intent_id,
                    "status": "Submitted",
                    "filled": 0,
                    "remaining": abs(request.signed_quantity),
                },
            )
        )
        return {"order_id": order_id, "perm_id": order_id}

    def _submit_ioc(self, request, order_id):
        """Immediate-or-cancel: fill at the touch up to displayed size, then cancel the rest.

        Never rests in self.orders, so demo fill loops cannot fill it later. A marketable buy
        (limit >= ask) fills at the ask and a marketable sell (limit <= bid) at the bid, with no
        price improvement; the final status reports the true cumulative filled quantity.
        """
        if request.con_id not in self.manifest.allowlist:
            raise ValueError("Discretionary trading of mandatory/unapproved instruments is forbidden")
        self.requests[order_id] = request
        reference = "hedge:" + request.intent_id
        signed = request.signed_quantity
        size = abs(signed)
        status = {
            "order_id": order_id,
            "perm_id": order_id,
            "order_ref": reference,
            "order_type": "LMT",
            "limit_price": float(request.limit_price),
        }
        self.events.put_nowait(("order", {**status, "status": "Submitted", "filled": 0, "remaining": size}))
        cap = int(self.ioc_fill_caps.pop(0)) if self.ioc_fill_caps else None
        quote = self._quotes.get(request.con_id)
        amount = 0
        if quote is not None:
            touch = quote.ask if signed > 0 else quote.bid
            limit = float(request.limit_price)
            marketable = limit >= quote.ask - 1e-9 if signed > 0 else limit <= quote.bid + 1e-9
            if marketable and touch > 0:
                amount = min(size, quote.ask_size if signed > 0 else quote.bid_size)
                if cap is not None:
                    amount = min(amount, max(0, cap))
        if amount:
            self._execute(
                order_id,
                request.con_id,
                amount if signed > 0 else -amount,
                Decimal(str(quote.ask if signed > 0 else quote.bid)),
                quote,
                reference,
            )
        complete = amount == size
        self.events.put_nowait(
            (
                "order",
                {
                    **status,
                    "status": "Filled" if complete else "Cancelled",
                    "filled": amount,
                    "remaining": 0 if complete else size - amount,
                },
            )
        )
        return {"order_id": order_id, "perm_id": order_id}

    def _execute(self, order_id, con_id, signed, price, quote, order_ref):
        """Book one synthetic execution with a unique exec id and its commission report."""
        c = self.manifest.contract_map[con_id]
        fee = max(
            self.manifest.policy.minimum_commission,
            abs(signed)
            * (
                self.manifest.policy.commission_option
                if c.kind == "OPT"
                else self.manifest.policy.commission_share
            ),
        )
        if c.kind == "STK":  # IBKR Fixed: capped at 1% of trade value
            fee = min(fee, 0.01 * abs(signed) * float(price))
        self.cash -= price * signed * c.multiplier + Decimal(str(fee))
        self.nav -= (price - Decimal(str(quote.mid))) * signed * c.multiplier + Decimal(str(fee))
        self.positions[con_id] = self.positions.get(con_id, 0) + signed
        self.filled[order_id] = self.filled.get(order_id, 0) + abs(signed)
        # Unique per execution: same-clock partial fills must never collide (store dedupes ids).
        self._exec_counter += 1
        execution = Execution(
            exec_id=f"SIM.{order_id}.{self._exec_counter}.{self.now.timestamp()}",
            order_id=order_id,
            account=self.manifest.account,
            con_id=con_id,
            signed_quantity=signed,
            price=price,
            at=self.now,
            order_ref=order_ref,
        )
        self.events.put_nowait(("execution", execution))
        self.events.put_nowait(
            ("commission", {"exec_id": execution.exec_id, "amount": str(fee), "currency": "USD"})
        )
        return execution

    async def fill(self, order_id, quantity=None):
        if order_id in self.package_orders:
            request = self.package_orders[order_id]
            amount = request.quantity if quantity is None else int(quantity)
            if not 0 < amount <= request.quantity:
                raise ValueError("Protected package overfill")
            prices = {
                request.buy_id: Decimal(str(self._quotes[request.buy_id].mid)),
                request.sell_id: Decimal(str(self._quotes[request.buy_id].mid)) - request.limit_price,
            }
            if any(
                not self._quotes[i].bid <= float(price) <= self._quotes[i].ask for i, price in prices.items()
            ):
                return
            self._counter += 1
            # Synthetic atomic ratio fills: both positions update before any event.
            for i, sign in [(request.buy_id, 1), (request.sell_id, -1)]:
                q = self._quotes[i]
                price = prices[i]
                fee = max(
                    self.manifest.policy.minimum_commission, amount * self.manifest.policy.commission_option
                )
                self.positions[i] = self.positions.get(i, 0) + sign * amount
                self.cash -= price * sign * amount * 100 + Decimal(str(fee))
                self.nav -= (price - Decimal(str(q.mid))) * sign * amount * 100 + Decimal(str(fee))
                execution = Execution(
                    exec_id=f"SIM.PACKAGE.{order_id}.{i}.{self._counter}.{self.now.timestamp()}",
                    order_id=order_id,
                    perm_id=order_id,
                    account=self.manifest.account,
                    con_id=i,
                    signed_quantity=sign * amount,
                    price=price,
                    at=self.now,
                    order_ref="hedge-package:" + request.package_id,
                )
                self.events.put_nowait(("execution", execution))
                self.events.put_nowait(
                    ("commission", {"exec_id": execution.exec_id, "amount": str(fee), "currency": "USD"})
                )
            remaining = request.quantity - amount
            if remaining:
                self.package_orders[order_id] = request.model_copy(update={"quantity": remaining})
                self.orders[order_id] = tuple(
                    o.model_copy(
                        update={"signed_remaining": remaining * (1 if o.con_id == request.buy_id else -1)}
                    )
                    for o in self.orders[order_id]
                )
            else:
                del self.orders[order_id]
                del self.package_orders[order_id]
            self.events.put_nowait(
                (
                    "order",
                    {
                        "order_id": order_id,
                        "perm_id": order_id,
                        "order_ref": "hedge-package:" + request.package_id,
                        "status": "Submitted" if remaining else "Filled",
                        "remaining": remaining,
                        "filled": amount,
                    },
                )
            )
            return
        order = self.orders[order_id]
        signed = (
            order.signed_remaining
            if quantity is None
            else int(quantity) * (1 if order.signed_remaining > 0 else -1)
        )
        if abs(signed) > abs(order.signed_remaining):
            raise ValueError("Overfill")
        quote = self._quotes[order.con_id]
        price = Decimal(str(quote.ask if signed > 0 else quote.bid))
        request = self.requests.get(order_id)
        if request is not None and request.order_type == "LMT":
            if (
                signed > 0
                and float(request.limit_price) < quote.bid
                or signed < 0
                and float(request.limit_price) > quote.ask
            ):
                return
            price = min(price, request.limit_price) if signed > 0 else max(price, request.limit_price)
        remaining = order.signed_remaining - signed
        if remaining:
            self.orders[order_id] = order.model_copy(update={"signed_remaining": remaining})
        else:
            del self.orders[order_id]
        self._execute(order_id, order.con_id, signed, price, quote, order.order_ref)
        self.events.put_nowait(
            (
                "order",
                {
                    "order_id": order_id,
                    "order_ref": order.order_ref,
                    "status": "Filled" if not remaining else "Submitted",
                    "remaining": abs(remaining),
                    "filled": abs(order.signed_remaining) - abs(remaining),
                },
            )
        )

    async def setup_mandate(self, request, order_id, now):
        raise ValueError("Bare mandatory purchases disabled; use a guaranteed protective package")

    async def submit_package(self, request, order_id, now):
        if self.readonly or not self.connected or request.account != self.manifest.account:
            raise ValueError("Synthetic protected setup unavailable")
        if (
            now.astimezone(__import__("zoneinfo").ZoneInfo("America/New_York")).date()
            != self.manifest.purchase_session
        ):
            raise ValueError("Outside purchase session")
        if (
            self.snapshot().projected().get(request.buy_id, 0) + request.quantity
            > self.manifest.mandatory[request.buy_id]
        ):
            raise ValueError("Protected package exceeds exact mandate")
        self.package_orders[order_id] = request
        self.orders[order_id] = tuple(
            WorkingOrder(
                order_id=order_id,
                perm_id=order_id,
                con_id=i,
                signed_remaining=sign * request.quantity,
                order_ref="hedge-package:" + request.package_id,
            )
            for i, sign in [(request.buy_id, 1), (request.sell_id, -1)]
        )
        self.events.put_nowait(
            (
                "order",
                {
                    "order_id": order_id,
                    "perm_id": order_id,
                    "order_ref": "hedge-package:" + request.package_id,
                    "status": "Submitted",
                    "remaining": request.quantity,
                    "filled": 0,
                },
            )
        )
        return {"order_id": order_id, "perm_id": order_id}

    async def cancel(self, order_id):
        order = self.orders.pop(order_id, None)
        self.package_orders.pop(order_id, None)
        if order:
            if isinstance(order, tuple):
                order = order[0]
            self.events.put_nowait(
                (
                    "order",
                    {
                        "order_id": order_id,
                        "order_ref": order.order_ref,
                        "status": "Cancelled",
                        "remaining": 0,
                        "filled": self.filled.get(order_id, 0),
                    },
                )
            )

    async def replace(self, order_id, limit_price):
        if order_id not in self.orders:
            raise ValueError("Unknown order")
        if order_id in self.package_orders:
            self.package_orders[order_id] = self.package_orders[order_id].model_copy(
                update={"limit_price": Decimal(str(limit_price))}
            )
        elif order_id in self.requests:
            self.requests[order_id] = self.requests[order_id].model_copy(
                update={"limit_price": Decimal(str(limit_price))}
            )

    async def disconnect(self):
        self.connected = self.reconciled = False
