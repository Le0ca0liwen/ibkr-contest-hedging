"""The IBKR Activity Statement's Open Positions section: the closing marks the contest scored.

Accepts the section copied from the statement web page (tab- or space-separated cells) or the
statement CSV export. Every row must satisfy its own arithmetic (quantity x multiplier x close =
value, to the cent), so a shifted column or a misread number fails loudly instead of importing a
wrong mark. Options are named like "SPY 27NOV26 770 C"; the 4-decimal close is kept unrounded."""

from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
OPTION = re.compile(
    r"^(?P<symbol>.+?) (?P<day>\d{1,2})(?P<month>[A-Z]{3})(?P<year>\d{2}) (?P<strike>\d+(?:\.\d+)?) (?P<right>[CP])$"
)
NUMBER = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
TICKER = re.compile(r"^[A-Z][A-Z0-9.]*$")  # a pasted line starting otherwise is a label or prose
# Web page columns after Symbol: Quantity, Mult, Cost Price, Cost Basis, Close Price, Value, Unrealized P/L
TEXT_NUMBERS = 7
# CSV columns from the third field when the export has no "Open Positions,Header" row
CSV_COLUMNS = (
    "DataDiscriminator",
    "Asset Category",
    "Currency",
    "Symbol",
    "Quantity",
    "Mult",
    "Cost Price",
    "Cost Basis",
    "Close Price",
    "Value",
    "Unrealized P/L",
    "Code",
)
CSV_REQUIRED = ("Symbol", "Quantity", "Mult", "Close Price", "Value")
CSV_SKIPPED = {"SubTotal", "Total", "Lot"}  # Lot rows repeat a Summary row's position
ABSOLUTE, RELATIVE = Decimal("0.01"), Decimal("1e-6")


def _number(text, where):
    text = text.strip()
    if not NUMBER.match(text):
        raise ValueError(f"{where}: {text!r} is not a number")
    return Decimal(text.replace(",", ""))


def _integer(text, name, where):
    value = _number(text, where)
    if value != value.to_integral_value():
        raise ValueError(f"{where}: {name} {text!r} is not a whole number")
    return int(value)


def _row(where, raw, symbol, quantity, multiplier, close, value, category=None):
    symbol = " ".join(symbol.split())
    option = OPTION.match(symbol)
    kind = "OPT" if option else "STK"
    if category is not None:
        expected = "OPT" if "Option" in category else "STK" if category == "Stocks" else None
        if expected is None:
            raise ValueError(f"{where}: unsupported asset category {category!r}")
        if expected != kind:
            raise ValueError(f"{where}: {symbol!r} is not a {category} symbol")
    row = {
        "symbol": symbol,
        "kind": kind,
        "expiry": None,
        "strike": None,
        "right": None,
        "quantity": _integer(quantity, "Quantity", where),
        "multiplier": _integer(multiplier, "Mult", where),
        "close": _number(close, where),
        "value": _number(value, where),
        "raw": raw,
    }
    if option:
        try:
            expiry = date(2000 + int(option["year"]), MONTHS.index(option["month"]) + 1, int(option["day"]))
        except ValueError:
            raise ValueError(f"{where}: {symbol!r} has no valid expiry") from None
        row.update(
            symbol=option["symbol"],
            expiry=expiry,
            strike=float(option["strike"]),
            right=option["right"],
        )
    elif row["multiplier"] != 1:
        raise ValueError(f"{where}: {symbol!r} is not a share or an option symbol like 'SPY 27NOV26 770 C'")
    computed = row["quantity"] * row["multiplier"] * row["close"]
    if abs(computed - row["value"]) > ABSOLUTE + RELATIVE * abs(row["value"]):
        raise ValueError(
            f"{where}: quantity x multiplier x close is {computed}, but the statement value is {row['value']}"
        )
    return row


def _text_rows(lines):
    rows = []
    for n, line in lines:
        cells = [c.strip() for c in line.split("\t")] if "\t" in line else line.split()
        cells = [c for c in cells if c]
        if not cells or not TICKER.match(cells[0].split()[0]) or not any(c.isdigit() for c in line):
            continue  # headers, section labels ("Stocks", "USD", ...) and "Total" lines
        where = f"line {n} {line.strip()!r}"
        while len(cells) > 1 and not NUMBER.match(cells[-1]):
            cells.pop()  # Code
        numbers = cells[-TEXT_NUMBERS:]
        if len(cells) <= TEXT_NUMBERS or not all(NUMBER.match(c) for c in numbers):
            raise ValueError(
                f"{where}: expected Symbol, Quantity, Mult, Cost Price, Cost Basis, Close Price, "
                "Value, Unrealized P/L"
            )
        quantity, multiplier, _, _, close, value, _ = numbers
        symbol = " ".join(cells[:-TEXT_NUMBERS])
        rows.append(_row(where, line.strip(), symbol, quantity, multiplier, close, value))
    return rows


def _cell(fields, columns, name):
    index = columns.get(name)
    return fields[index].strip() if index is not None and index < len(fields) else ""


def _fields(line):
    """A CSV line's fields when it is an Open Positions record (quoted or not), else None."""
    fields = next(csv.reader([line]), [])
    return fields if len(fields) > 1 and fields[0].strip() == "Open Positions" else None


def _csv_rows(lines):
    columns, rows = {name: i for i, name in enumerate(CSV_COLUMNS, 2)}, []
    for n, line in lines:
        fields = _fields(line)
        if fields is None:
            continue
        where = f"line {n} {line.strip()!r}"
        if fields[1] == "Header":
            columns = {name.strip(): i for i, name in enumerate(fields) if i >= 2}
            missing = [name for name in CSV_REQUIRED if name not in columns]
            if missing:
                raise ValueError(f"{where}: no {', '.join(missing)} column")
            continue
        symbol = _cell(fields, columns, "Symbol")
        skipped = _cell(fields, columns, "DataDiscriminator") in CSV_SKIPPED or symbol.startswith("Total")
        if fields[1] != "Data" or skipped:
            continue
        currency = _cell(fields, columns, "Currency")
        if currency and currency != "USD":
            raise ValueError(f"{where}: only USD positions are supported")
        category = _cell(fields, columns, "Asset Category") if "Asset Category" in columns else None
        rows.append(
            _row(
                where,
                line.strip(),
                symbol,
                _cell(fields, columns, "Quantity"),
                _cell(fields, columns, "Mult"),
                _cell(fields, columns, "Close Price"),
                _cell(fields, columns, "Value"),
                category,
            )
        )
    return rows


def parse_open_positions(text):
    """Open Positions rows, from the pasted web page section or the CSV export:
    {"symbol", "kind", "expiry", "strike", "right", "quantity", "multiplier", "close" (Decimal),
    "value" (Decimal), "raw"}. An option's symbol is its underlying. ValueError names a line that
    cannot be read or whose quantity x multiplier x close is not its value, and when no position
    row is found at all (an unrecognized paste must not import as zero holdings)."""
    lines = list(enumerate(text.lstrip("\ufeff").splitlines(), 1))
    if any(_fields(line) for _, line in lines):
        rows = _csv_rows(lines)
    else:
        rows = _text_rows(lines)
    if not rows:
        raise ValueError(
            "no Open Positions rows found: paste the section's position lines or the statement CSV"
        )
    return rows


def _same(contract, row):
    if contract.kind != row["kind"] or contract.symbol != row["symbol"]:
        return False
    return contract.kind == "STK" or (
        contract.expiry == row["expiry"]
        and contract.right == row["right"]
        and abs(contract.strike - row["strike"]) < 1e-6
    )


def match_contracts(manifest, rows):
    """({con_id: row}, [rows without a manifest contract]). ValueError when a row matches several
    contracts, two rows match one, or the statement's multiplier differs from the manifest's."""
    matched, unmatched = {}, []
    for row in rows:
        found = [c for c in manifest.contracts if _same(c, row)]
        if not found:
            unmatched.append(row)
            continue
        where = repr(row["raw"].strip())
        if len(found) > 1:
            raise ValueError(f"{where} matches contracts {[c.con_id for c in found]}")
        contract = found[0]
        if contract.con_id in matched:
            raise ValueError(
                f"{where} and {matched[contract.con_id]['raw'].strip()!r} are both {contract.label}"
            )
        if contract.multiplier != row["multiplier"]:
            raise ValueError(f"{where}: multiplier {row['multiplier']}, manifest {contract.multiplier}")
        matched[contract.con_id] = row
    return matched, unmatched


def _plain(value):
    """A Decimal as text with at least two decimals and no trailing zeros beyond them."""
    places = min(value.normalize().as_tuple().exponent, -2)
    return str(value.quantize(Decimal(1).scaleb(places)))


def statement_closes(manifest, text):
    """The statement's closing marks, JSON-safe: {"rows": {str(con_id): {"label", "quantity",
    "close", "value"}}, "unmatched": [raw lines], "stock_value", "option_value"}. The totals sum
    every row of that kind, matched or not; prices and values are decimal strings."""
    rows = parse_open_positions(text)
    matched, unmatched = match_contracts(manifest, rows)
    labels = {c.con_id: c.label for c in manifest.contracts}
    totals = {"STK": Decimal(0), "OPT": Decimal(0)}
    for row in rows:
        totals[row["kind"]] += row["value"]
    return {
        "rows": {
            str(con_id): {
                "label": labels[con_id],
                "quantity": row["quantity"],
                "close": _plain(row["close"]),
                "value": _plain(row["value"]),
            }
            for con_id, row in matched.items()
        },
        "unmatched": [row["raw"] for row in unmatched],
        "stock_value": _plain(totals["STK"]),
        "option_value": _plain(totals["OPT"]),
    }
