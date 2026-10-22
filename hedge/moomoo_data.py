"""Quote-only streaming adapter; live eligibility is independently risk gated."""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from .config import NY
from .models import Quote
from .valuation import BelowModelFloor, ValuationEngine


def code_for(contract):
    if contract.kind == "STK":
        return f"US.{contract.symbol}"
    return f"US.{contract.symbol}{contract.expiry:%y%m%d}{contract.right}{round(contract.strike * 1000)}"


def collector_codes(manifest):
    """Collector subscription list: underlyings first, then mandated legs, then candidates."""
    contracts = manifest.contracts
    underlyings = {c.symbol for c in contracts if c.kind == "OPT"} | {manifest.beta_benchmark}
    stocks = sorted({f"US.{s}" for s in underlyings} | {code_for(c) for c in contracts if c.kind == "STK"})
    mandated = [code_for(manifest.contract_map[i]) for i in sorted(manifest.mandatory)]
    others = sorted(code_for(c) for c in contracts if c.kind == "OPT" and c.con_id not in manifest.mandatory)
    return list(dict.fromkeys([*stocks, *mandated, *others]))


def missing_codes(manifest, path="config/local.moomoo-codes.json"):
    """Manifest contracts the running collector will never subscribe (no quotes, silently untradable)."""
    try:
        subscribed = set(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        subscribed = set()
    return [code for code in collector_codes(manifest) if code not in subscribed]


class MoomooData:
    def __init__(self, manifest, path="data/moomoo/latest.json"):
        self.path = Path(path)
        self.manifest = manifest
        self.engine = ValuationEngine(manifest)
        self.received_at = None
        self.errors = []
        self.subscription_failed = []  # collector codes whose Moomoo subscription failed
        self.last_iv = {}

    @staticmethod
    def book_time(item):
        if item.get("book_push_count", 0) < 2:
            return None
        at = datetime.fromisoformat(item["book_push_at"])
        if at.tzinfo is None:
            return None
        book = item["order_book"]
        bid, ask = float(book["Bid"][0][0]), float(book["Ask"][0][0])
        if not all(math.isfinite(x) for x in (bid, ask)) or bid <= 0 or ask < bid:
            raise ValueError("Invalid two-sided book")
        # Source timestamps are not guaranteed for US instruments. When supplied,
        # retain their conservative age as additional evidence, never overwrite it.
        times = []
        for field in ("svr_recv_time_bid", "svr_recv_time_ask"):
            value = book.get(field)
            if value and not value.startswith("1970"):
                source = datetime.fromisoformat(value)
                times.append(source.replace(tzinfo=NY) if source.tzinfo is None else source)
        # Each side reports its last exchange update. An unchanged bid must
        # not make a fresh ask/book update stale; date the complete snapshot by
        # the latest side update, bounded by actual callback arrival.
        return min(at, max(times)) if times else at

    def quotes(self, now):
        try:
            raw = json.loads(self.path.read_text())
            if raw.get("purpose") != "market_data_only" or raw.get("execution_enabled") is not False:
                raise ValueError("Untrusted feed purpose")
            received = datetime.fromisoformat(raw["received_at"])
            if received.tzinfo is None or not 0 <= (now - received).total_seconds() <= 90:
                raise ValueError("Moomoo collector stale or invalid timestamp")
            self.received_at = received.isoformat()
            securities = raw["securities"]
            quotes, errors = {}, []
            self.subscription_failed = sorted(
                code for code, item in securities.items() if isinstance(item, dict) and item.get("error")
            )
            if self.subscription_failed:
                errors.append(
                    "Moomoo subscription failed: "
                    + ", ".join(self.subscription_failed)
                    + " (the quote collector retries every 30 s)"
                )
            for contract in self.manifest.contracts:
                try:
                    item = securities[code_for(contract)]
                    row = item["quote"][0]
                    book = item["order_book"]
                    if row.get("data_date") and row.get("data_time"):
                        at = datetime.fromisoformat(row["data_date"] + "T" + row["data_time"]).replace(
                            tzinfo=NY
                        )
                    else:
                        # No trade yet today: blank last-trade fields. The book still counts, dated by
                        # its receipt time.
                        at = self.book_time(item)
                        if at is None:
                            raise ValueError("No provider timestamp")
                    stock_item = securities[f"US.{contract.symbol}"]
                    stock_book = stock_item["order_book"]
                    spot = (float(stock_book["Bid"][0][0]) + float(stock_book["Ask"][0][0])) / 2
                    stock_at = self.book_time(stock_item)
                    bid, ask = float(book["Bid"][0][0]), float(book["Ask"][0][0])
                    live = False
                    book_at = at
                    if raw.get("quote_server_connected") and item.get("book_push_count", 0) >= 2:
                        book_at = self.book_time(item)
                        permission = raw.get("rights", {}).get(
                            "us_option_qot_right" if contract.kind == "OPT" else "us_qot_right"
                        )
                        # A quiet (unchanged) option book stays valid up to the held-leg bound;
                        # the risk gate still requires option_quote_age for anything traded.
                        max_age = (
                            self.manifest.risk.held_option_quote_age
                            if contract.kind == "OPT"
                            else self.manifest.risk.stock_quote_age
                        )
                        live = (
                            permission in ("LV1", "LV2", "LV3", "SF")
                            and raw.get("rights", {}).get("us_qot_right") in ("LV1", "LV2", "LV3", "SF")
                            and stock_item.get("book_push_count", 0) >= 2
                            and stock_at is not None
                            and timedelta(0)
                            <= now - stock_at
                            <= timedelta(seconds=self.manifest.risk.stock_quote_age)
                            and book_at is not None
                            and timedelta(0) <= now - book_at <= timedelta(seconds=max_age)
                            and self.manifest.session_open(now.astimezone(NY).date())
                            <= now
                            < self.manifest.session_close(now.astimezone(NY).date())
                        )
                    iv, greeks = None, {}
                    if contract.kind == "OPT":
                        if (
                            float(row["strike_price"]) != contract.strike
                            or int(row["contract_multiplier"]) != contract.multiplier
                        ):
                            raise ValueError("Provider contract identity mismatch")
                        iv = float(row["implied_volatility"]) / 100
                        identity = raw.get("identities", {}).get(code_for(contract), {})
                        live = live and (
                            identity.get("stock_owner") == f"US.{contract.symbol}"
                            and identity.get("strike_time") == str(contract.expiry)
                            and identity.get("option_type") == ("CALL" if contract.right == "C" else "PUT")
                            and float(identity.get("strike_price", 0)) == contract.strike
                        )
                        if live:
                            # Last trade and vendor IV may be old despite a current book.
                            # Calibrate the American model to the executable midpoint.
                            vendor = iv if iv and math.isfinite(iv) and iv > 0 else None
                            try:
                                iv = self.engine.implied_volatility(contract, spot, (bid + ask) / 2, now)
                                self.last_iv[contract.con_id] = iv
                            except BelowModelFloor as exc:
                                # Deep ITM: price ~ intrinsic and delta ~ +/-1 regardless of IV, so a
                                # carried IV keeps a valid, fresh book live instead of halting hedging.
                                # Only for a tight book around the floor; a nonsense or stub-bid book
                                # (mid far below intrinsic) stays frozen, or NAV would swing on it.
                                iv = self.last_iv.get(contract.con_id) or vendor
                                spread_cap = self.manifest.execution.option_spread_absolute
                                if ask < exc.floor - 0.01 or bid < exc.floor - spread_cap:
                                    live, iv = False, vendor
                                    errors.append(f"Model calibration blocked {contract.label}: {exc}")
                                elif iv is None:
                                    live = False
                                    errors.append(
                                        f"Model calibration blocked {contract.label}: no IV to carry"
                                    )
                                else:
                                    errors.append(
                                        f"Deep-ITM midpoint at model floor; carried IV for {contract.label}"
                                    )
                            except ValueError as exc:
                                live = False
                                errors.append(f"Model calibration blocked {contract.label}: {exc}")
                        greeks = self.engine.greeks(contract, spot, iv, now if live else at)
                    # Last-trade time is not the timestamp of this executable book.
                    # Vendor clocks can be slightly ahead of the callback clock;
                    # a verified current book still uses its REAL bounded receipt
                    # timestamp. Never revive an unverified/cached future trade.
                    if not live and at > now:
                        raise ValueError("Future unverified provider timestamp")
                    quotes[contract.con_id] = Quote(
                        provider="moomoo",
                        con_id=contract.con_id,
                        bid=bid,
                        ask=ask,
                        bid_size=int(book["Bid"][0][1]),
                        ask_size=int(book["Ask"][0][1]),
                        observed_at=book_at if book_at is not None else at,
                        data_type="live" if live else "frozen",
                        spot=spot,
                        iv=iv,
                        greek_at=min(book_at, stock_at) if live and iv else at if iv else None,
                        model_price=self.engine.price(contract, spot, iv, now) if live and iv else None,
                        shortable_shares=self.manifest.stock_borrow.get(contract.symbol)
                        if contract.kind == "STK"
                        else None,
                        **greeks,
                    )
                    if not live:
                        errors.append(f"Non-executable moomoo book/model: {contract.label}")
                except (KeyError, IndexError, TypeError, ValueError):
                    errors.append(f"Missing or invalid moomoo data: {contract.label}")
            self.errors = errors
            return quotes
        except (OSError, KeyError, TypeError, ValueError):
            self.errors = ["Moomoo feed unavailable, stale, or invalid; no executable data"]
            self.subscription_failed = []
            return {}
