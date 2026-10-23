"""
Fill one quarter of an oil & gas company from its own disclosures - CSV and MongoDB together.

    python update_disclosure.py BSR --quarter 3 --year 2026            # parse, write CSV + CongTyDauKhi
    python update_disclosure.py BSR --quarter 3 --year 2026 --dry-run  # parse and show, write nothing
    python update_disclosure.py GAS --quarter 2 --year 2026 --overwrite

Each ticker has a parser, disclosures/<ticker>.py, exposing
    collect(q, y) -> disclosures.common.Result(values={CSV column: number}, sources, warnings)
which finds the company's documents for that quarter (consolidated financial
statements, investor bulletins, operating reports - see the module docstring),
reads them and returns the columns of company_<ticker>_quarterly.csv it could fill.
CongTyDauKhi.update_quarter then writes the row to the CSV and upserts the same
quarter into dc_commodity.CongTyDauKhi - no CSV-then-load step.

Headline P&L comes from the DC Database first: the skill runs
    SELECT KEYCODE, VALUE FROM FA_Quarterly WHERE TICKER = '<T>' AND DATE = '<Y>Q<q>'
through the DC Database MCP, saves the rows as JSON and passes --fa-json; those
numbers (structured, available within hours of the filing) take priority over
what the parser read from the PDF, which then only has to supply segments,
volumes and operating data.

By default only empty cells are filled: numbers already in the CSV (the analyst's
Diamond history) are kept, and any that disagree with the disclosure by more than
1% are listed as conflicts. --overwrite replaces them.

The per-ticker Claude skills (bsr-disclosure-updater, ...) run this script.
"""

import argparse
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from CongTyDauKhi import SPECS, update_quarter  # noqa: E402
from disclosures.common import label, to_bn  # noqa: E402

# FA_Quarterly KEYCODE -> CSV column (VND -> VND bn)
FA_COLUMNS = {"Net_Revenue": "Net revenue (VNDbn)", "Gross_Profit": "Gross profit (VNDbn)"}


def read_fa(path):
    """FA_Quarterly rows saved from the MCP (a list of {KEYCODE, VALUE}, or the tool's whole
    response with them under "data") -> {CSV column: VND bn}."""
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    rows = data.get("data", data) if isinstance(data, dict) else data
    raw = {r["KEYCODE"]: r["VALUE"] for r in rows if r.get("VALUE") is not None}
    return {col: to_bn(raw[k]) for k, col in FA_COLUMNS.items() if k in raw}


def main():
    parser = argparse.ArgumentParser(description="Disclosures -> company_<ticker>_quarterly.csv + CongTyDauKhi")
    parser.add_argument("ticker", type=str.upper, choices=list(SPECS))
    parser.add_argument("--quarter", type=int, required=True, choices=[1, 2, 3, 4])
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", help="parse and print, write nothing")
    parser.add_argument("--overwrite", action="store_true", help="replace cells that already have a value")
    parser.add_argument("--fa-json", help="FA_Quarterly rows of this ticker/quarter from the DC Database MCP")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    module = importlib.import_module(f"disclosures.{args.ticker.lower()}")
    q = label(args.quarter, args.year)
    print(f"== {args.ticker} {q}")
    result = module.collect(args.quarter, args.year)
    if args.fa_json:
        fa = read_fa(args.fa_json)
        for col, v in fa.items():
            pdf = result.values.get(col)
            if pdf is not None and abs(pdf - v) > max(1.0, abs(v) * 0.005):
                result.warn(f"{col}: PDF {pdf:,.1f} vs FA_Quarterly {v:,.1f} - dung FA_Quarterly")
        # derived columns (segment plugs, per-litre margins) are recomputed by the parser from these
        result.values.update(fa)
        if hasattr(module, "derive"):
            module.derive(result, args.quarter, args.year)
        result.sources.insert(0, "DC Database FA_Quarterly")
    for src in result.sources:
        print(f"  nguon: {src}")
    if not result.values:
        print("  Khong trich duoc so nao - tai lieu ky nay co the chua cong bo.")
        for w in result.warnings:
            print(f"  ! {w}")
        return 1

    filled, conflicts = update_quarter(args.ticker, q, result.values, "; ".join(result.sources),
                                       overwrite=args.overwrite, dry_run=args.dry_run)
    width = max(len(c) for c in result.values)
    for col, v in result.values.items():
        mark = "+" if col in filled else ("!" if col in conflicts else "=")
        note = f"  (CSV dang co {conflicts[col][0]:g})" if col in conflicts else ""
        print(f"  {mark} {col:<{width}}  {'—' if v is None else f'{v:,.2f}'}{note}")
    for w in result.warnings:
        print(f"  REVIEW: {w}")
    verb = "se ghi" if args.dry_run else "da ghi CSV + MongoDB"
    print(f"  {len(filled)} o {verb}; {len(conflicts)} o lech voi so dang co (giu nguyen, --overwrite de thay)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
