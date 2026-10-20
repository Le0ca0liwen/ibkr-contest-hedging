"""Read-only qualification of additional US equities/ETFs and their options; no order interface."""

import re
from datetime import date
from decimal import Decimal

from ib_async import IB, Option, RequestError, Stock

from .models import Contract
from .practice import paper_account
from .universe import neighbouring_expiries


def _symbols(symbols):
    symbols = tuple(sorted(set(symbols)))
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", s) for s in symbols):
        raise ValueError("Provide valid US stock/ETF symbols")
    return symbols


async def _stock(broker, symbol):
    details = await broker.reqContractDetailsAsync(Stock(symbol, "SMART", "USD"))
    if len(details) != 1:
        raise ValueError(f"Ambiguous/unavailable US stock: {symbol}")
    c = details[0].contract
    if c.symbol != symbol or c.secType != "STK" or c.currency != "USD" or not c.conId:
        raise ValueError(f"Instrument identity mismatch: {symbol}")
    return Contract(con_id=c.conId, symbol=symbol, kind="STK")


async def _option_details(broker, symbol, expiry, strike, right):
    """Contract details, or [] when IBKR has no such strike at this expiry (error 200)."""
    try:
        return await broker.reqContractDetailsAsync(
            Option(
                symbol,
                expiry.strftime("%Y%m%d"),
                strike,
                right,
                "SMART",
                multiplier="100",
                currency="USD",
                tradingClass=symbol,
            )
        )
    except RequestError as exc:
        if exc.code == 200:  # Weeklies often omit strikes listed on the monthly chain.
            return []
        raise ValueError(f"IBKR contract request failed ({exc.code}): {exc.message}") from None


def _option(details, symbol, underlying_id, expiry, strike, right):
    if len(details) != 1:
        return None
    native = details[0].contract
    if (
        native.symbol != symbol
        or native.secType != "OPT"
        or native.currency != "USD"
        or native.lastTradeDateOrContractMonth[:8] != expiry.strftime("%Y%m%d")
        or native.strike != strike
        or native.right != right
        or Decimal(native.multiplier or "0") != 100
        or getattr(details[0], "underConId", 0) != underlying_id
    ):
        raise ValueError(f"{symbol} {expiry} {strike}{right} identity/multiplier mismatch")
    return Contract(
        con_id=native.conId,
        symbol=symbol,
        kind="OPT",
        multiplier=100,
        expiry=expiry,
        strike=strike,
        right=right,
        trading_class=native.tradingClass,
        underlying_id=underlying_id,
    )


def _expanded(manifest, additions):
    contracts = (*manifest.contracts, *additions)
    data = {**manifest.model_dump(), "contracts": contracts}
    if manifest.universe == "allowlist":
        # Forbidden instruments (e.g. SPY shares) stay defined for quotes but never become tradable.
        data["allowlist"] = (
            *manifest.allowlist,
            *(c.con_id for c in additions if not manifest.prohibited_reason(c)),
        )
    return type(manifest).model_validate(data)


async def qualify_stock_universe(
    manifest, symbols, ib=None, option_symbols=(), expiries=2, strikes=6, centers=None
):
    symbols, option_symbols = _symbols(symbols), _symbols(option_symbols)
    if not symbols and not option_symbols:
        raise ValueError("Provide at least one stock/ETF or option symbol")
    if not 1 <= expiries <= 6 or not 1 <= strikes <= 20:
        raise ValueError("Option discovery takes 1-6 expiries and 1-20 strikes per expiry")
    broker = ib or IB()
    broker.RaiseRequestErrors = True
    broker.RequestTimeout = 15
    additions = []
    try:
        await broker.connectAsync(
            "127.0.0.1", manifest.gateway_port, clientId=83, readonly=True, timeout=15, raiseSyncErrors=True
        )
        paper_account(broker.managedAccounts(), manifest.account)
        stocks = {c.symbol: c for c in manifest.contracts if c.kind == "STK"}
        for symbol in sorted(set(symbols) | set(option_symbols)):
            if symbol not in stocks:
                stocks[symbol] = await _stock(broker, symbol)
                additions.append(stocks[symbol])
        if len(stocks) > 12:
            raise ValueError("At most 12 underlying symbols per validated optimization universe")
        known = {(c.symbol, c.expiry, c.strike, c.right) for c in manifest.contracts if c.kind == "OPT"}
        for symbol in option_symbols:
            additions += await _option_chain(
                manifest, broker, stocks[symbol], expiries, strikes, (centers or {}).get(symbol), known
            )
        return _expanded(manifest, additions)
    finally:
        broker.disconnect()


async def _option_chain(manifest, broker, stock, expiries, strikes, center, known):
    symbol = stock.symbol
    if center is None:
        legs = [manifest.contract_map[i] for i in manifest.mandatory]
        mandated = [c.strike for c in legs if c.symbol == symbol and c.kind == "OPT"]
        if not mandated:
            raise ValueError(f"Provide a reference price for {symbol} option discovery")
        center = sum(mandated) / len(mandated)
    chains = [
        c
        for c in await broker.reqSecDefOptParamsAsync(symbol, "", "STK", stock.con_id)
        if c.exchange == "SMART" and c.tradingClass == symbol and Decimal(c.multiplier or "0") == 100
    ]
    if len(chains) != 1:
        raise ValueError(f"Standard {symbol} option chain is ambiguous/unavailable")
    # Options must outlive the final scored session; take the expiries nearest the mandated one
    # (both sides), which is what screen_options considers.
    legs = [manifest.contract_map[i] for i in manifest.mandatory]
    mandated = sorted(c.expiry for c in legs if c.symbol == symbol and c.kind == "OPT")
    last = manifest.scoring_sessions[-1]
    eligible = [date.fromisoformat(f"{e[:4]}-{e[4:6]}-{e[6:8]}") for e in sorted(chains[0].expirations)]

    def placeholder(expiry, strike, right):
        return Contract(
            con_id=1,
            symbol=symbol,
            kind="OPT",
            multiplier=100,
            expiry=expiry,
            strike=strike,
            right=right,
            underlying_id=stock.con_id,
        )

    # Skip expiries where every new contract would be forbidden (SPY on the contest expiry).
    eligible = [
        e
        for e in eligible
        if e >= last and not all(manifest.prohibited_reason(placeholder(e, center, r)) for r in "CP")
    ]
    primary = mandated[0] if mandated else manifest.contest_option_expiry or (eligible or [last])[0]
    chosen = ([primary] if primary in eligible else []) + neighbouring_expiries(eligible, primary)
    chosen = chosen[:expiries]
    near = sorted((k for k in chains[0].strikes if k > 0), key=lambda k: (abs(k - center), k))[:strikes]
    found = []
    for expiry in sorted(chosen):
        for strike in sorted(near):
            for right in ("C", "P"):
                if (symbol, expiry, strike, right) in known:
                    continue
                # Forbidden contest options (Nov 20 RKLB calls, extra Nov 20 SPY options) are never requested.
                if manifest.prohibited_reason(placeholder(expiry, strike, right)):
                    continue
                details = await _option_details(broker, symbol, expiry, strike, right)
                c = _option(details, symbol, stock.con_id, expiry, strike, right)
                if c is not None and not manifest.prohibited_reason(c):
                    found.append(c)
    return found


async def prepare_contest(template, account, strikes, report_source, port=4002, ib=None):
    """Read-only: qualify the 3 underlyings and 7 mandated legs from the final strikes.

    `template` is the parsed contest template dict; `strikes` maps SPCX to its 3 call strikes,
    RKLB to its 2 put strikes and SPY to the straddle strike. Signs follow the contest structure.
    """
    from .config import Manifest

    expiry = date.fromisoformat(str(template["contest_option_expiry"]))
    spcx, rklb, spy = sorted(strikes["SPCX"]), sorted(strikes["RKLB"]), strikes["SPY"]
    if len(spcx) != 3 or len(rklb) != 2 or len(spy) != 1:
        raise ValueError("Need 3 SPCX call strikes, 2 RKLB put strikes and 1 SPY strike")
    legs = [
        ("SPCX", spcx[0], "C", 25),
        ("SPCX", spcx[1], "C", -50),
        ("SPCX", spcx[2], "C", 25),
        ("RKLB", rklb[0], "P", -25),
        ("RKLB", rklb[1], "P", 25),
        ("SPY", spy[0], "C", 25),
        ("SPY", spy[0], "P", 25),
    ]
    broker = ib or IB()
    broker.RaiseRequestErrors = True
    broker.RequestTimeout = 15
    try:
        await broker.connectAsync(
            "127.0.0.1", port, clientId=84, readonly=True, timeout=15, raiseSyncErrors=True
        )
        account = paper_account(broker.managedAccounts(), account)
        stocks = {symbol: await _stock(broker, symbol) for symbol in ("SPCX", "RKLB", "SPY")}
        contracts, mandatory = list(stocks.values()), {}
        for symbol, strike, right, quantity in legs:
            details = await _option_details(broker, symbol, expiry, strike, right)
            c = _option(details, symbol, stocks[symbol].con_id, expiry, strike, right)
            if c is None:
                raise ValueError(f"Ambiguous/unavailable {symbol} {expiry} {strike}{right}")
            contracts.append(c)
            mandatory[c.con_id] = quantity
        data = {
            **template,
            "account": account,
            "report_source": report_source,
            "gateway_port": port,
            "contracts": [c.model_dump(mode="json") for c in contracts],
            "mandatory": mandatory,
            "confirmed": False,
        }
        return Manifest.model_validate(data)
    finally:
        broker.disconnect()
