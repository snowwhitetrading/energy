"""
Unattended run of the oil & gas disclosure updaters (scheduled in GitHub Actions).

For every ticker and every quarter still "open" - ended at least WAIT days ago and at most
GIVE_UP days ago - it checks company_<ticker>_quarterly.csv: when one of the ticker's DONE
columns is still empty, it runs disclosures/<ticker>.py and writes what it found to the CSV
and dc_commodity.CongTyDauKhi (CongTyDauKhi.update_quarter, empty cells only - the analyst's
numbers are never overwritten). A quarter whose DONE columns are all filled is skipped
without touching the network, so a daily run costs nothing between reporting seasons.

Not available here: DC Database FA_Quarterly (MCP only) - the headline comes from the
P&L page of the BCTC instead, which matches FA_Quarterly to the VND.

    python auto_disclosure.py                  # all tickers, open quarters
    python auto_disclosure.py --tickers BSR    # one ticker
    python auto_disclosure.py --quarter 2 --year 2026 --dry-run   # force one quarter, write nothing

Writes a Markdown summary to $GITHUB_STEP_SUMMARY when set (else stdout). Exit code 0 even
when documents are not out yet; 1 only when a parser crashed.
"""

import argparse
import importlib
import os
import sys
import traceback
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from CongTyDauKhi import SPECS, read_csv, update_quarter  # noqa: E402
from disclosures.common import label  # noqa: E402

WAIT = 20      # days after quarter end before looking (BCTCs come out ~28-30 days after)
GIVE_UP = 120  # stop chasing a quarter after this many days (bulletins come 1-5 weeks after the BCTC)

# a quarter is complete once these are filled (the parser's financial + operating outputs)
DONE = {
    "BSR": ["Net revenue (VNDbn)", "DO net revenue (VNDbn)", "Total Volume (k tons)"],
    "GAS": ["Net revenue (VNDbn)", "LPG net revenue (VNDbn)", "Total Volume (mn m3)"],
    "PLX": ["Net revenue (VNDbn)", "Inventory provision (VNDbn)"],
    "OIL": ["Net revenue (VNDbn)", "Services net revenue (VNDbn)"],
    "PVS": ["Net revenue (VNDbn)", "M&C net revenue (VNDbn)", "JV & associates income (VNDbn)"],
    "PVD": ["Net revenue (VNDbn)", "Drilling net revenue (VNDbn)"],
}


def quarter_end(q, y):
    return date(y, 12, 31) if q == 4 else date(y, 3 * q + 1, 1) - timedelta(days=1)


def open_quarters(today):
    """(q, y) whose end is between WAIT and GIVE_UP days before today, oldest first."""
    out = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(3):
        q, y = (4, y - 1) if q == 1 else (q - 1, y)
        age = (today - quarter_end(q, y)).days
        if WAIT <= age <= GIVE_UP:
            out.append((q, y))
    return sorted(out, key=lambda qy: (qy[1], qy[0]))


def missing(ticker, quarter):
    _, rows = read_csv(ticker)
    row = dict(rows).get(quarter, {})
    return [c for c in DONE[ticker] if row.get(c) is None]


def main():
    parser = argparse.ArgumentParser(description="Scheduled disclosure updates for the oil & gas tickers")
    parser.add_argument("--tickers", nargs="*", default=list(DONE), type=str.upper)
    parser.add_argument("--quarter", type=int, choices=[1, 2, 3, 4])
    parser.add_argument("--year", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    quarters = [(args.quarter, args.year)] if args.quarter and args.year else open_quarters(date.today())
    lines = [f"## Oil & gas disclosures - {date.today():%Y-%m-%d}", ""]
    if not quarters:
        lines.append("No quarter in its reporting window - nothing to do.")
    crashed = False
    for q, y in quarters:
        quarter = label(q, y)
        for ticker in args.tickers:
            if ticker not in SPECS:
                continue
            todo = missing(ticker, quarter)
            forced = bool(args.quarter)
            if not todo and not forced:
                lines.append(f"- **{ticker} {quarter}**: complete")
                continue
            print(f"== {ticker} {quarter} (missing: {', '.join(todo) or '-'})", flush=True)
            try:
                result = importlib.import_module(f"disclosures.{ticker.lower()}").collect(q, y)
            except Exception:  # noqa: BLE001 - one broken parser must not stop the others
                crashed = True
                lines.append(f"- **{ticker} {quarter}**: parser error\n\n```\n{traceback.format_exc()[-1500:]}\n```")
                continue
            if not result.values:
                why = result.warnings[-1] if result.warnings else "no figures"
                lines.append(f"- **{ticker} {quarter}**: not published yet ({why})")
                continue
            filled, conflicts = update_quarter(ticker, quarter, result.values, "; ".join(result.sources),
                                               dry_run=args.dry_run)
            verb = "would fill" if args.dry_run else "filled"
            lines.append(f"- **{ticker} {quarter}**: {verb} {len(filled)} cells"
                         + (f", {len(conflicts)} differ from the CSV (kept)" if conflicts else ""))
            for col, (old, new) in filled.items():
                lines.append(f"  - `{col}` = {new:,.2f}")
            for col, (old, new) in conflicts.items():
                lines.append(f"  - ! `{col}`: CSV {old:,.2f} vs disclosure {new:,.2f}")
            for w in result.warnings:
                lines.append(f"  - REVIEW: {w}")
            for s in result.sources:
                lines.append(f"  - source: {s}")

    text = "\n".join(lines) + "\n"
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main())
