"""Importing the IBKR Activity Statement's Open Positions: the closing marks the contest scored."""

import csv
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from hedge.models import Contract
from hedge.statement_import import match_contracts, parse_open_positions, statement_closes

# The Oct 22 2026 statement's Open Positions as copied from the web page (cost columns are
# illustrative except RKLB's; quantity, multiplier, close and value are the statement's).
HEADER = (
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
STOCK_ROWS = [
    ("RKLB", "1,120", "1", "51.234980679", "57,383.18", "51.2200", "57,366.40", "-16.78", " "),
    ("SPCX", "119", "1", "141.5", "16,838.50", "144.7500", "17,225.25", "386.75", " "),
]
OPTION_ROWS = [
    ("RKLB 20NOV26 45 P", "-25", "100", "1.379620922", "-3,449.05", "1.9300", "-4,825.00", "-1,375.95", " "),
    ("RKLB 20NOV26 55 P", "25", "100", "7.02", "17,550.00", "6.5500", "16,375.00", "-1,175.00", " "),
    ("SPCX 20NOV26 130 C", "25", "100", "16.25", "40,625.00", "18.4774", "46,193.50", "5,568.50", " "),
    ("SPCX 20NOV26 145 C", "-50", "100", "8.10", "-40,500.00", "9.0497", "-45,248.50", "-4,748.50", " "),
    ("SPCX 20NOV26 145 P", "1", "100", "9.40", "940.00", "8.8000", "880.00", "-60.00", " "),
    ("SPCX 20NOV26 160 C", "25", "100", "3.15", "7,875.00", "3.7000", "9,250.00", "1,375.00", " "),
    ("SPY 20NOV26 770 C", "25", "100", "10.12", "25,300.00", "10.7400", "26,850.00", "1,550.00", " "),
    ("SPY 20NOV26 770 P", "25", "100", "14.90", "37,250.00", "14.4150", "36,037.50", "-1,212.50", " "),
    ("SPY 27NOV26 770 C", "-17", "100", "12.40", "-21,080.00", "12.1150", "-20,595.50", "484.50", " "),
    ("SPY 27NOV26 770 P", "-20", "100", "15.02", "-30,040.00", "15.2400", "-30,480.00", "-440.00", " "),
]
PAGE = [
    ("Open Positions",),
    HEADER,
    ("Stocks",),
    ("USD",),
    *STOCK_ROWS,
    ("Total", " ", " ", " ", "74,221.68", " ", "74,591.65", "369.97", " "),
    ("Equity and Index Options",),
    ("USD",),
    *OPTION_ROWS,
    ("Total", " ", " ", " ", "34,470.95", " ", "34,437.00", "-33.95", " "),
    ("Carried by Interactive Brokers LLC",),
]
TABBED = "\n".join("\t".join(cells) for cells in PAGE)
# A browser copy: empty cells come through as non-breaking spaces, lines end in CRLF.
BROWSER = "\r\n".join("\t".join("\xa0" if cell == " " else cell for cell in cells) for cells in PAGE)
SPACED = "\n".join(" ".join(cells) for cells in PAGE)
PADDED = "\n".join("".join(cell.ljust(21) for cell in cells).rstrip() for cells in PAGE)

CSV = """\ufeffStatement,Header,Field Name,Field Value
Statement,Data,Period,"October 22, 2026"
Net Asset Value,Header,Asset Class,Prior Total,Current Long,Current Short,Current Total,Change
Net Asset Value,Data,Stock,0,74591.65,0,74591.65,74591.65
Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code
Open Positions,Data,Summary,Stocks,USD,RKLB,1120,1,51.234980679,57383.18,51.22,57366.4,-16.78,
Open Positions,Data,Summary,Stocks,USD,SPCX,119,1,141.5,16838.5,144.75,17225.25,386.75,
Open Positions,Total,,Stocks,USD,,,,,74221.68,,74591.65,369.97,
Open Positions,Data,Summary,Equity and Index Options,USD,RKLB 20NOV26 45 P,-25,100,1.379620922,-3449.05,1.93,-4825,-1375.95,
Open Positions,Data,Summary,Equity and Index Options,USD,RKLB 20NOV26 55 P,25,100,7.02,17550,6.55,16375,-1175,
Open Positions,Data,Summary,Equity and Index Options,USD,SPCX 20NOV26 130 C,25,100,16.25,40625,18.4774,46193.5,5568.5,
Open Positions,Data,Summary,Equity and Index Options,USD,SPCX 20NOV26 145 C,-50,100,8.1,-40500,9.0497,-45248.5,-4748.5,
Open Positions,Data,Summary,Equity and Index Options,USD,SPCX 20NOV26 145 P,1,100,9.4,940,8.8,880,-60,
Open Positions,Data,Summary,Equity and Index Options,USD,SPCX 20NOV26 160 C,25,100,3.15,7875,3.7,9250,1375,
Open Positions,Data,Summary,Equity and Index Options,USD,SPY 20NOV26 770 C,25,100,10.12,25300,10.74,26850,1550,
Open Positions,Data,Summary,Equity and Index Options,USD,SPY 20NOV26 770 P,25,100,14.9,37250,14.415,36037.5,-1212.5,
Open Positions,Data,Summary,Equity and Index Options,USD,SPY 27NOV26 770 C,-17,100,12.4,-21080,12.115,-20595.5,484.5,
Open Positions,Data,Summary,Equity and Index Options,USD,SPY 27NOV26 770 P,-20,100,15.02,-30040,15.24,-30480,-440,
Open Positions,Data,SubTotal,Equity and Index Options,USD,,,,,34470.95,,34437,-33.95,
Open Positions,Total,,Equity and Index Options,USD,,,,,34470.95,,34437,-33.95,
Mark-to-Market Performance Summary,Header,Asset Category,Symbol,Prior Quantity,Current Quantity
"""
# The same export with every field quoted, as some exports and spreadsheet re-saves write it.
QUOTED = "".join('"' + '","'.join(fields) + '"\n' for fields in csv.reader(CSV.lstrip("\ufeff").splitlines()))

EXPIRY, LATER = date(2026, 11, 20), date(2026, 11, 27)
CONTRACTS = {  # con_id: (symbol, expiry, strike, right); statement close, value
    1: ("RKLB", None, None, None, "51.22", "57366.40"),
    2: ("SPCX", None, None, None, "144.75", "17225.25"),
    11: ("RKLB", EXPIRY, 45, "P", "1.93", "-4825.00"),
    12: ("RKLB", EXPIRY, 55, "P", "6.55", "16375.00"),
    13: ("SPCX", EXPIRY, 130, "C", "18.4774", "46193.50"),
    14: ("SPCX", EXPIRY, 145, "C", "9.0497", "-45248.50"),
    15: ("SPCX", EXPIRY, 145, "P", "8.80", "880.00"),
    16: ("SPCX", EXPIRY, 160, "C", "3.70", "9250.00"),
    17: ("SPY", EXPIRY, 770, "C", "10.74", "26850.00"),
    18: ("SPY", EXPIRY, 770, "P", "14.415", "36037.50"),
    19: ("SPY", LATER, 770, "C", "12.115", "-20595.50"),
    20: ("SPY", LATER, 770, "P", "15.24", "-30480.00"),
}


def contract(con_id, symbol, expiry=None, strike=None, right=None, multiplier=100):
    if expiry is None:
        return Contract(con_id=con_id, symbol=symbol, kind="STK")
    return Contract(
        con_id=con_id,
        symbol=symbol,
        kind="OPT",
        multiplier=multiplier,
        expiry=expiry,
        strike=strike,
        right=right,
    )


def contest_manifest(*extra, leave_out=()):
    contracts = [contract(i, *spec[:4]) for i, spec in CONTRACTS.items() if i not in leave_out]
    return SimpleNamespace(contracts=(*contracts, *extra))


@pytest.mark.parametrize(
    "text",
    [TABBED, BROWSER, SPACED, PADDED, CSV, QUOTED],
    ids=["tabs", "browser", "spaces", "padded", "csv", "quoted csv"],
)
def test_the_oct_22_open_positions_parse_from_every_form(text):
    rows = parse_open_positions(text)
    assert len(rows) == 12
    assert sum(r["value"] for r in rows if r["kind"] == "STK") == Decimal("74591.65")
    assert sum(r["value"] for r in rows if r["kind"] == "OPT") == Decimal("34437.00")
    rklb, spcx = rows[:2]
    assert {
        k: rklb[k] for k in ("symbol", "kind", "expiry", "strike", "right", "quantity", "multiplier")
    } == {
        "symbol": "RKLB",
        "kind": "STK",
        "expiry": None,
        "strike": None,
        "right": None,
        "quantity": 1120,
        "multiplier": 1,
    }
    assert (rklb["close"], rklb["value"]) == (Decimal("51.22"), Decimal("57366.40"))
    assert (spcx["symbol"], spcx["quantity"], spcx["close"]) == ("SPCX", 119, Decimal("144.75"))
    short_call = rows[-2]
    assert (short_call["symbol"], short_call["kind"], short_call["expiry"]) == ("SPY", "OPT", LATER)
    assert (short_call["strike"], short_call["right"], short_call["quantity"]) == (770.0, "C", -17)
    assert (short_call["multiplier"], short_call["close"]) == (100, Decimal("12.115"))
    assert short_call["value"] == Decimal("-20595.50")
    assert rows[4]["close"] == Decimal("18.4774")  # the 4-decimal close is kept, never rounded
    assert "SPY 27NOV26 770 C" in short_call["raw"]


def test_the_statement_example_lines_parse_verbatim():
    text = (
        "RKLB\t1,120\t1\t51.234980679\t57,383.18\t51.2200\t57,366.40\t-16.78\t \n"
        "RKLB 20NOV26 45 P\t-25\t100\t1.379620922\t-3,449.05\t1.9300\t-4,825.00\t-1,375.95"
    )
    stock, put = parse_open_positions(text)
    assert (stock["symbol"], stock["quantity"], stock["value"]) == ("RKLB", 1120, Decimal("57366.40"))
    assert stock["raw"] == "RKLB\t1,120\t1\t51.234980679\t57,383.18\t51.2200\t57,366.40\t-16.78"
    assert (put["symbol"], put["expiry"], put["strike"], put["right"]) == ("RKLB", EXPIRY, 45.0, "P")
    assert (put["quantity"], put["multiplier"], put["close"]) == (-25, 100, Decimal("1.93"))


def test_decimal_strikes_and_multiple_spaces_inside_the_symbol():
    (row,) = parse_open_positions(
        "RKLB  20NOV26   52.5  C    10   100   2.10   2,100.00   2.3500   2,350.00   250.00"
    )
    assert (row["symbol"], row["strike"], row["right"], row["value"]) == (
        "RKLB",
        52.5,
        "C",
        Decimal("2350.00"),
    )
    matched, unmatched = match_contracts(
        SimpleNamespace(contracts=(contract(7, "RKLB", EXPIRY, 52.5, "C"),)), [row]
    )
    assert matched == {7: row} and unmatched == []


@pytest.mark.parametrize(
    "text",
    [
        TABBED.replace("-45,248.50", "-45,248.60"),
        CSV.replace("9.0497,-45248.5", "9.0497,-45249.5"),
        TABBED.replace("9.0497", "9.05"),  # 9.05 x -5,000 is 2.50 off the statement's value
    ],
    ids=["value", "csv value", "rounded close"],
)
def test_a_row_whose_value_does_not_reconcile_raises_naming_the_line(text):
    with pytest.raises(ValueError, match="SPCX 20NOV26 145 C") as error:
        parse_open_positions(text)
    assert "line " in str(error.value)


def test_a_cent_of_rounding_is_tolerated_and_a_shifted_column_is_not():
    assert parse_open_positions(TABBED.replace("880.00", "880.01"))[6]["value"] == Decimal("880.01")
    with pytest.raises(ValueError, match="SPCX 20NOV26 145 P"):
        parse_open_positions(TABBED.replace("880.00", "880.02"))
    # plus 1e-6 of the value: 0.067 at 57,366.40
    assert parse_open_positions(TABBED.replace("57,366.40", "57,366.46"))[0]["value"] == Decimal("57366.46")
    with pytest.raises(ValueError, match="RKLB"):
        parse_open_positions(TABBED.replace("57,366.40", "57,366.48"))
    with pytest.raises(ValueError, match="line 5"):  # Cost Basis missing: every column shifts
        parse_open_positions(TABBED.replace("\t57,383.18\t", "\t"))
    with pytest.raises(ValueError, match="not a share or an option symbol"):
        parse_open_positions("RKLB 261120P00045000\t-25\t100\t1.38\t-3,449.05\t1.9300\t-4,825.00\t-1,375.95")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Open Positions\nSymbol\tQuantity\tMult\nStocks\nUSD\nCarried by Interactive Brokers LLC",
        TABBED.lower(),
        CSV.replace(",", "\t"),  # the export pasted from a spreadsheet
        "\n".join(line for line in CSV.splitlines() if not line.startswith("Open Positions,Data")),
    ],
    ids=["empty", "labels only", "lowercase", "spreadsheet paste", "csv without data rows"],
)
def test_text_without_a_position_row_raises_instead_of_importing_nothing(text):
    with pytest.raises(ValueError, match="no Open Positions rows found"):
        parse_open_positions(text)
    with pytest.raises(ValueError, match="no Open Positions rows found"):
        statement_closes(contest_manifest(), text)


def test_csv_columns_follow_the_header():
    lines = [
        "Open Positions,Header,DataDiscriminator,Asset Category,Symbol,Currency,Mult,Quantity,Value,Close Price,Code",
        "Open Positions,Data,Summary,Stocks,RKLB,USD,1,1120,57366.4,51.22,",
        "Open Positions,Data,Lot,Stocks,RKLB,USD,1,1120,57366.4,51.22,",
        "Open Positions,Data,Summary,Equity and Index Options,SPY 27NOV26 770 P,USD,100,-20,-30480,15.24,",
    ]
    stock, put = parse_open_positions("\n".join(lines))
    assert (stock["symbol"], stock["quantity"], stock["close"]) == ("RKLB", 1120, Decimal("51.22"))
    assert (put["symbol"], put["expiry"], put["right"], put["quantity"]) == ("SPY", LATER, "P", -20)
    without_header = "\n".join(line for line in CSV.splitlines() if "Open Positions,Header" not in line)
    assert len(parse_open_positions(without_header)) == 12
    with pytest.raises(ValueError, match="no Mult, Close Price, Value column"):
        parse_open_positions(
            "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity\n"
        )
    with pytest.raises(ValueError, match="only USD"):
        parse_open_positions(CSV.replace("Stocks,USD,SPCX", "Stocks,CAD,SPCX"))
    with pytest.raises(ValueError, match="is not a Equity and Index Options symbol"):
        parse_open_positions(CSV.replace("RKLB 20NOV26 55 P", "RKLB"))


@pytest.mark.parametrize(
    "text", [TABBED, BROWSER, PADDED, CSV, QUOTED], ids=["tabs", "browser", "padded", "csv", "quoted csv"]
)
def test_statement_closes_match_every_manifest_contract(text):
    result = statement_closes(contest_manifest(contract(30, "SPY", EXPIRY, 780, "C")), text)
    assert result["stock_value"] == "74591.65"
    assert result["option_value"] == "34437.00"
    assert result["unmatched"] == []
    assert set(result["rows"]) == {str(i) for i in CONTRACTS}
    for con_id, (*_, close, value) in CONTRACTS.items():
        assert (result["rows"][str(con_id)]["close"], result["rows"][str(con_id)]["value"]) == (close, value)
    assert result["rows"]["11"] == {
        "label": "RKLB 2026-11-20 45P",
        "quantity": -25,
        "close": "1.93",
        "value": "-4825.00",
    }
    assert result["rows"]["1"] == {"label": "RKLB", "quantity": 1120, "close": "51.22", "value": "57366.40"}
    assert result["rows"]["19"]["label"] == "SPY 2026-11-27 770C"


def test_rows_without_a_manifest_contract_stay_in_the_totals():
    result = statement_closes(contest_manifest(leave_out=(2, 15)), TABBED)
    assert set(result["rows"]) == {str(i) for i in CONTRACTS} - {"2", "15"}
    assert [line.split("\t")[0] for line in result["unmatched"]] == ["SPCX", "SPCX 20NOV26 145 P"]
    assert (result["stock_value"], result["option_value"]) == ("74591.65", "34437.00")


def test_matching_is_by_symbol_expiry_strike_and_right():
    rows = parse_open_positions(TABBED)
    matched, unmatched = match_contracts(contest_manifest(), rows)
    assert {i: r["raw"].split("\t")[0] for i, r in matched.items()} == {
        i: symbol if expiry is None else f"{symbol} {expiry:%d%b%y}".upper() + f" {strike} {right}"
        for i, (symbol, expiry, strike, right, *_) in CONTRACTS.items()
    }
    assert unmatched == []
    near = SimpleNamespace(
        contracts=(
            contract(40, "SPY", LATER, 770.01, "C"),
            contract(41, "SPY", EXPIRY, 770, "C"),
            contract(42, "SPY", LATER, 770, "P"),
            contract(43, "SPX", LATER, 770, "C"),
            Contract(con_id=44, symbol="SPY", kind="STK"),
        )
    )
    _, unmatched = match_contracts(near, [r for r in rows if r["raw"].startswith("SPY 27NOV26 770 C")])
    assert len(unmatched) == 1


def test_duplicate_matches_raise():
    rows = parse_open_positions(TABBED)
    with pytest.raises(ValueError, match="matches contracts"):
        match_contracts(contest_manifest(contract(50, "SPY", LATER, 770, "P")), rows)
    with pytest.raises(ValueError, match="are both SPY 2026-11-20 770C"):
        match_contracts(contest_manifest(), [*rows, rows[8]])
    with pytest.raises(ValueError, match="multiplier 100, manifest 10"):
        match_contracts(
            SimpleNamespace(contracts=(contract(51, "SPY", EXPIRY, 770, "C", multiplier=10),)), rows
        )
