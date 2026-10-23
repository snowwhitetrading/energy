"""
Oil & gas company quarterly tables, built from the analysts' Diamond workbooks.

For each covered ticker this script reads its "<TICKER>-Diamond*.xlsx" workbook
(cached cell values, never formulas), picks the quarterly rows listed in SPECS
below and writes:

1. company_<ticker>_quarterly.csv next to this file - the same layout as the
   power company files in Q:/Coding/utility (Date = "2Q2026", one column per
   series named "<segment/product> <metric> (<unit>)", money in VNDbn);
2. dc_commodity.CongTyDauKhi - one document per ticker and quarter, in the
   shape ThiTruongDien uses for company_* datasets:
      {dataset: "company_<ticker>_quarterly", ticker, date (quarter end),
       date_text: "2Q2026", frequency: "quarterly", values: {column: number}}
   The commo dashboard (Oil & Gas tab) reads this collection.

Only reported quarters are kept: every ticker has a LAST actual quarter, and the
forecast columns to the right of it in the workbook are ignored. Bump "last"
in SPECS when a new quarter is filled in.

The workbooks are gitignored; the CSVs are tracked, so CI (or anyone without
the workbooks) can reload MongoDB from them with --from-csv.

Usage:
    python CongTyDauKhi.py                    # all tickers -> CSV + MongoDB
    python CongTyDauKhi.py --tickers BSR GAS
    python CongTyDauKhi.py --no-mongo         # CSV only (dry run)
    python CongTyDauKhi.py --from-csv         # upload the CSVs as they are
"""

import argparse
import csv
import glob
import hashlib
import os
import re
import sys
import warnings
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ThiTruongKhoan import DATABASE_NAME, connect_mongo, upsert  # noqa: E402

COLLECTION_NAME = "CongTyDauKhi"
QUARTER = re.compile(r"^([1-4])Q(20\d\d)$")


def qkey(label):
    """'2Q2026' -> 20262, for range comparisons."""
    m = QUARTER.match(label)
    return int(m.group(2)) * 10 + int(m.group(1))


def quarter_end(label):
    m = QUARTER.match(label)
    q, y = int(m.group(1)), int(m.group(2))
    return datetime(y, q * 3, (31, 30, 30, 31)[q - 1], tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# per-ticker specs
# --------------------------------------------------------------------------
# Each spec names its sheets as {alias: (sheet, header row)}; the header row holds the
# quarter of every column, as a label ("1Q19", "Q1 2019", "2Q26A") or a quarter-end date.
# A series is (column, row) or (column, row, options):
#   sheet         sheet alias (default "main")
#   scale         multiply the cell (0.001 tons -> k tons, 100 fraction -> %)
#   minus         rows subtracted from `row` in the same column (revenue - COGS = GP)
#   first / last  quarters the series is valid for (inclusive)
#   only          explicit set of quarters to keep
#   override      {quarter: number | None | (sheet alias, row) | (sheet name, "D105")}
#                 for broken cells, or values kept outside the quarterly grid
#   col_from      {quarter: other quarter} - read that quarter from another column
#                 (a note block shifted by one quarter)
# A derived series is (column, fn): fn(values of the quarter so far, quarter) -> number | None.

def plug(total, parts):
    """Balancing item: total minus the listed parts, only when every part is present."""
    def fn(v, q):
        if v.get(total) is None or any(v.get(p) is None for p in parts):
            return None
        return v[total] - sum(v[p] for p in parts)
    return fn


def ratio(num, den, scale=1.0):
    def fn(v, q):
        a, b = v.get(num), v.get(den)
        return None if a is None or not b else a / b * scale
    return fn


NR, GP = "Net revenue (VNDbn)", "Gross profit (VNDbn)"


def segments(names, nr_rows, gp_rows=None, cogs_rows=None, opts=None):
    """`<seg> net revenue` + `<seg> gross profit` columns for a list of segments."""
    out = [(f"{n} net revenue (VNDbn)", r, dict(opts or {})) for n, r in zip(names, nr_rows)]
    if gp_rows:
        out += [(f"{n} gross profit (VNDbn)", r, dict(opts or {})) for n, r in zip(names, gp_rows)]
    elif cogs_rows:
        out += [(f"{n} gross profit (VNDbn)", r, {**(opts or {}), "minus": [c]})
                for n, r, c in zip(names, nr_rows, cogs_rows)]
    return out


BSR_PRODUCTS = ["DO", "A95", "A92", "JetA1", "LPG", "Polypropylene", "FO"]

GAS_SEGMENTS = ["Dry gas & LNG", "LPG", "CNG", "Transportation", "Condensate", "Others", "Office leasing"]
PVS_SEGMENTS = ["OSV", "FSO/FPSO", "ROV & geophysical", "Supply base & ports", "M&C", "O&M", "Others"]
PVD_SEGMENTS = ["Drilling", "Well services", "Trading"]

# PVD jack-up day rate after 3Q2024 lives in the Schedule sheet: one row per year,
# quarters in columns D / G / J / M.
PVD_DAYRATE = {f"{q + 1}Q{y}": ("Schedule", f"{c}{row}")
               for y, row in ((2024, 75), (2025, 105), (2026, 128)) for q, c in enumerate("DGJM")}
PVD_DAYRATE = {q: v for q, v in PVD_DAYRATE.items() if qkey(q) >= qkey("4Q2024")}

SPECS = {
    "BSR": {
        "name": "Binh Son Refining",
        "sheets": {"main": ("QoQ", 4)}, "first": "1Q2019", "last": "2Q2026",
        "series": [
            (NR, 7), (GP, 9),
            *[(f"{p} net revenue (VNDbn)", r) for p, r in zip(BSR_PRODUCTS, range(121, 128))],
            ("Others net revenue (VNDbn)", plug(NR, [f"{p} net revenue (VNDbn)" for p in BSR_PRODUCTS])),
            # product GP is complete from 1Q2024 (1Q2022 / 1Q2023 exist but overshoot the total by ~2,300bn)
            *[(f"{p} gross profit (VNDbn)", r, {"first": "1Q2024"}) for p, r in zip(BSR_PRODUCTS, range(145, 152))],
            ("Others gross profit (VNDbn)", plug(GP, [f"{p} gross profit (VNDbn)" for p in BSR_PRODUCTS])),
            # labelled k tons, stored in tons
            ("Total Volume (k tons)", 67, {"scale": 0.001}),
            *[(f"{p} Volume (k tons)", r, {"scale": 0.001, "last": "2Q2025"})
              for p, r in zip(BSR_PRODUCTS + ["Others"], range(77, 85))],
            # later quarters of the production row copy earlier ones
            ("Production (k tons)", 66, {"scale": 0.001, "first": "1Q2020", "last": "3Q2025"}),
            ("Utilisation (%)", 72, {"scale": 100, "first": "1Q2020", "last": "3Q2025"}),
            ("Asian 2-1-1 crack spread (USD/bbl)", 156),
            ("Brent (USD/bbl)", 155, {"first": "1Q2018"}),
        ],
    },
    "GAS": {
        "name": "PV Gas",
        "sheets": {"main": ("QoQ", 1), "fs": ("Quarter FS", 1)}, "first": "1Q2019", "last": "2Q2026",
        "series": [
            (NR, 124, {"sheet": "fs"}), (GP, 126, {"sheet": "fs"}),
            # quarterly segments start in 1Q2024; segment revenue is before sales deductions
            *segments(GAS_SEGMENTS, range(67, 74), range(85, 92), opts={"first": "1Q2024"}),
            # dry gas sold: the total and its split by customer group (4Q2024 is a copy of 1Q2024)
            ("Total Volume (mn m3)", 118, {"override": {"4Q2024": None}}),
            ("Power Volume (mn m3)", 119, {"override": {"4Q2024": None}}),
            ("Fertilizer Volume (mn m3)", 120, {"override": {"4Q2024": None}}),
            ("Industrial & CNG Volume (mn m3)", 121, {"override": {"4Q2024": None}}),
            ("LNG input (mn m3)", 110, {"first": "1Q2025"}),
            ("LPG Volume (k tons)", 123),
            ("LPG production (k tons)", 114),
            ("Condensate Volume (k tons)", 113),
            ("Brent (USD/bbl)", 2, {"first": "1Q2021"}),
        ],
    },
    "PLX": {
        "name": "Petrolimex",
        # no quarterly segment split in the workbook - totals and domestic volume only
        "sheets": {"main": ("QoQ", 2), "fs": ("Quarter FS", 1)}, "first": "1Q2019", "last": "2Q2026",
        "series": [
            (NR, 124, {"sheet": "fs"}), (GP, 126, {"sheet": "fs"}),
            ("Domestic Volume (k m3)", 63, {"scale": 1000, "first": "1Q2022"}),
            ("Unit gross profit (VND/litre)", ratio(GP, "Domestic Volume (k m3)", 1000)),
            ("Inventory provision (VNDbn)", 317, {"sheet": "fs"}),
        ],
    },
    "OIL": {
        "name": "PV OIL",
        "sheets": {"main": ("Quarter FS", 1), "qoq": ("QoQ", 2)}, "first": "1Q2019", "last": "2Q2026",
        "series": [
            (NR, 124), (GP, 126),
            # the note block is one quarter late in 2025: 1Q25 sits in the 2Q25 column, 2Q25 is missing
            ("Services net revenue (VNDbn)", 300, {"col_from": {"1Q2025": "2Q2025"}, "override": {"2Q2025": None}}),
            ("Services gross profit (VNDbn)", 300,
             {"minus": [312], "col_from": {"1Q2025": "2Q2025"}, "override": {"2Q2025": None}}),
            ("Fuel trading net revenue (VNDbn)", plug(NR, ["Services net revenue (VNDbn)"])),
            # inventory provisions and sales deductions land in the trading line, so the segments add up
            ("Fuel trading gross profit (VNDbn)", plug(GP, ["Services gross profit (VNDbn)"])),
            # 1Q23 is an estimate (1Q24 / 1.22); 2024 quarters come from year-to-date subtraction
            ("Total Volume (k m3)", 34, {"sheet": "qoq", "first": "1Q2024"}),
            ("Unit gross profit (VND/litre)", ratio(GP, "Total Volume (k m3)", 1000)),
            ("Inventory provision (VNDbn)", 317),
        ],
    },
    "PVS": {
        "name": "PTSC",
        "sheets": {"main": ("FS", 4), "fs": ("Quarter FS", 1)}, "first": "1Q2019", "last": "2Q2026",
        "series": [
            (NR, 6),
            # FS!3Q2020 points at the wrong column for most rows - take it from Quarter FS
            (GP, 8, {"override": {"3Q2020": ("fs", 126)}}),
            *segments(PVS_SEGMENTS, range(77, 84), cogs_rows=range(99, 106)),
            ("JV & associates income (VNDbn)", 25, {"override": {"3Q2020": ("fs", 130)}}),
        ],
    },
    "PVD": {
        "name": "PV Drilling",
        "sheets": {"main": ("QoQ VND", 3)}, "first": "1Q2019", "last": "2Q2026",
        "series": [
            (NR, 6), (GP, 8),
            *segments(PVD_SEGMENTS, range(81, 84), range(96, 99), opts={"first": "1Q2021"}),
            ("Jack-up day rate (USD/day)", 69, {"first": "3Q2022", "override": PVD_DAYRATE}),
            ("Jack-up utilisation (%)", 70, {"scale": 100, "first": "3Q2022", "last": "3Q2024"}),
        ],
    },
}


# --------------------------------------------------------------------------
# workbook reading
# --------------------------------------------------------------------------

LABEL = re.compile(r"^(?:([1-4])Q|Q([1-4])[\s/-]*)((?:20)?\d\d)$", re.I)


def quarter_of(cell):
    """Header cell -> '1Q2019' for labels '1Q19', 'Q1 2019', 'Q1/2019', '2Q26A' or a
    quarter-end date (some sheets stamp the 30th of March). Forecast labels ('3Q26F')
    map to None so a forecast column can never pass for an actual one."""
    if isinstance(cell, datetime):
        if cell.month % 3 == 0 and cell.day >= 28:
            return f"{cell.month // 3}Q{cell.year}"
        return None
    if cell is None:
        return None
    text = str(cell).strip().replace("\u00a0", " ")
    if text[-1:].upper() in ("F", "E"):
        return None
    m = LABEL.match(text[:-1] if text[-1:].upper() == "A" else text)
    if not m:
        return None
    year = m.group(3)
    return f"{m.group(1) or m.group(2)}Q{year if len(year) == 4 else '20' + year}"


def find_workbook(ticker):
    hits = sorted(glob.glob(os.path.join(HERE, f"{ticker}-Diamond*.xlsx")), key=os.path.getmtime)
    if not hits:
        raise RuntimeError(f"khong tim thay {ticker}-Diamond*.xlsx trong {HERE}")
    return hits[-1]  # newest copy if several


def read_sheets(path, names):
    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # print-area / data-validation noise
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return {n: [list(r) for r in book[n].iter_rows(values_only=True)] for n in names}
    finally:
        book.close()


def quarter_columns(grid, header_row):
    """{quarter: 0-based column}; first occurrence wins (annual blocks repeat labels further right)."""
    cols = {}
    for j, cell in enumerate(grid[header_row - 1]):
        q = quarter_of(cell)
        if q and q not in cols:
            cols[q] = j
    return cols


def number(cell):
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    return None  # '#REF!', '-', text


def raw_cell(grid, ref):
    """'D105' -> value of that cell."""
    m = re.match(r"^([A-Z]+)(\d+)$", ref)
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    line = grid[int(m.group(2)) - 1]
    return number(line[col - 1]) if col - 1 < len(line) else None


def build_table(ticker, spec, path):
    extra = {o[0] for e in spec["series"] if len(e) > 2
             for o in e[2].get("override", {}).values() if isinstance(o, tuple) and isinstance(o[1], str)}
    grids = read_sheets(path, {s for s, _ in spec["sheets"].values()} | extra)
    sheets = {alias: (grids[name], quarter_columns(grids[name], header_row))
              for alias, (name, header_row) in spec["sheets"].items()}
    lo, hi = qkey(spec["first"]), qkey(spec["last"])
    quarters = sorted((q for q in sheets["main"][1] if lo <= qkey(q) <= hi), key=qkey)
    if not quarters or quarters[-1] != spec["last"]:
        raise RuntimeError(f"{ticker}: sheet {spec['sheets']['main'][0]} khong co cot quy toi {spec['last']}")

    def cell(alias, row, q):
        grid, cols = sheets[alias]
        j = cols.get(q)
        line = grid[row - 1] if row - 1 < len(grid) else []
        return number(line[j]) if j is not None and j < len(line) else None

    table = {q: {} for q in quarters}
    columns = []
    for entry in spec["series"]:
        name, src = entry[0], entry[1]
        opts = entry[2] if len(entry) > 2 else {}
        columns.append(name)
        for q in quarters:
            k = qkey(q)
            if callable(src):
                v = src(table[q], q)
            elif q in opts.get("override", {}):
                o = opts["override"][q]
                if isinstance(o, tuple):
                    v = raw_cell(grids[o[0]], o[1]) if isinstance(o[1], str) else cell(o[0], o[1], q)
                else:
                    v = o
            elif ("first" in opts and k < qkey(opts["first"])) or ("last" in opts and k > qkey(opts["last"])) \
                    or ("only" in opts and q not in opts["only"]):
                v = None
            else:
                alias, at = opts.get("sheet", "main"), opts.get("col_from", {}).get(q, q)
                v = cell(alias, src, at)
                for r in opts.get("minus", ()):
                    m = cell(alias, r, at)
                    v = None if v is None or m is None else v - m
                if v is not None and "scale" in opts:
                    v *= opts["scale"]
            table[q][name] = None if v is None else round(v, 4)

    # leading quarters with nothing at all are padding
    while quarters and all(v is None for v in table[quarters[0]].values()):
        table.pop(quarters.pop(0))
    return columns, [(q, table[q]) for q in quarters]


# --------------------------------------------------------------------------
# CSV + MongoDB
# --------------------------------------------------------------------------

def csv_path(ticker):
    return os.path.join(HERE, f"company_{ticker.lower()}_quarterly.csv")


def fmt(v):
    if v is None:
        return ""
    return str(int(v)) if float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")


def write_csv(ticker, columns, rows):
    with open(csv_path(ticker), "w", newline="", encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["Date"] + columns)
        for q, values in rows:
            w.writerow([q] + [fmt(values.get(c)) for c in columns])


def read_csv(ticker):
    with open(csv_path(ticker), newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = []
        for cells in reader:
            if not cells or not QUARTER.match(cells[0].strip()):
                continue
            values = {}
            for c, raw in zip(header[1:], cells[1:]):
                raw = raw.replace(",", "").strip()
                try:
                    values[c] = float(raw) if raw else None
                except ValueError:
                    values[c] = None
            rows.append((cells[0].strip(), values))
    return header[1:], rows


UNIT = re.compile(r"\(([^)]*)\)\s*$")


def to_documents(ticker, columns, rows, source_file):
    dataset = f"company_{ticker.lower()}_quarterly"
    key = {c: c.replace(".", "_").lstrip("$") for c in columns}  # Mongo field names
    units = {key[c]: m.group(1) for c in columns if (m := UNIT.search(c))}
    now = datetime.now(timezone.utc)
    return [{
        "_id": hashlib.md5(f"{dataset}|{q}|{ticker}".encode("utf-8")).hexdigest(),
        "dataset": dataset,
        "frequency": "quarterly",
        "date": quarter_end(q),
        "date_text": q,
        "ticker": ticker,
        "name": SPECS[ticker]["name"],
        "object": ticker,
        "values": {key[c]: values.get(c) for c in columns},
        "units": units,
        "source_file": source_file,
        "updated_at": now,
    } for q, values in rows]


def main():
    parser = argparse.ArgumentParser(description="Diamond workbooks -> company_<ticker>_quarterly.csv + CongTyDauKhi")
    parser.add_argument("--tickers", nargs="*", default=list(SPECS), type=str.upper)
    parser.add_argument("--no-mongo", action="store_true", help="write the CSVs only")
    parser.add_argument("--from-csv", action="store_true", help="skip the workbooks, upload the CSVs as they are")
    args = parser.parse_args()

    loads = []
    for ticker in args.tickers:
        if ticker not in SPECS:
            raise SystemExit(f"khong co spec cho {ticker} (co: {', '.join(SPECS)})")
        if args.from_csv:
            columns, rows = read_csv(ticker)
            source = os.path.basename(csv_path(ticker))
        else:
            path = find_workbook(ticker)
            columns, rows = build_table(ticker, SPECS[ticker], path)
            write_csv(ticker, columns, rows)
            source = os.path.basename(path)
        filled = sum(1 for _, v in rows for x in v.values() if x is not None)
        print(f"{ticker}: {len(rows)} quy {rows[0][0]} -> {rows[-1][0]}, {len(columns)} cot, {filled} o co so  [{source}]")
        loads.append((ticker, to_documents(ticker, columns, rows, source)))

    if args.no_mongo:
        print("--no-mongo set, nothing written to MongoDB.")
        return 0

    client = connect_mongo()
    try:
        collection = client[DATABASE_NAME][COLLECTION_NAME]
        collection.create_index([("ticker", 1), ("date", 1)])
        for ticker, docs in loads:
            # a reload replaces the ticker's table, so dropped quarters do not linger
            removed = collection.delete_many({"ticker": ticker, "_id": {"$nin": [d["_id"] for d in docs]}}).deleted_count
            ins, mod = upsert(collection, docs, ["_id"])
            print(f"  {COLLECTION_NAME} {ticker}: {ins} inserted, {mod} updated, {removed} removed")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
