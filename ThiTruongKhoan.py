"""
Westwood Energy offshore JACKUP rig scraper.

Scrapes two Westwood infographic pages and loads the results into MongoDB
(cluster IRIS, database dc_commodity).
Only jackup data is collected - semisubmersible and drillship figures on the same
pages are deliberately ignored.

Both series land in the single collection dc_commodity.ThiTruongKhoan, told
apart by the "dataset" field:

1. "Weekly Global Offshore Rig Counts"
   https://www.westwoodenergy.com/news/infographics/weekly-global-offshore-rig-counts
   -> dataset "weekly_jackup_rig_count", one document per week:
      values = {"Jackup Rig Count": n}

2. "Offshore Energy Data Dashboard"
   https://www.westwoodenergy.com/news/infographics/offshore-energy-data-dashboard
   -> dataset "monthly_jackup_utilisation", one document per month:
      values = {"Committed Jackup Rig Count", "Marketed Committed Utilisation",
                "Total Utilisation", "Contract Backlog"}

Both loads are idempotent upserts, so the weekly scheduled run refreshes and
extends the series instead of duplicating it.

Usage:
    python ThiTruongKhoan.py                 # current-year page only
    python ThiTruongKhoan.py --years 2024 2025
    python ThiTruongKhoan.py --all           # every archived year + current
    python ThiTruongKhoan.py --no-mongo      # parse only (dry run)
"""

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.westwoodenergy.com/news/infographics"
RIG_COUNT_SLUG = "weekly-global-offshore-rig-counts"
DASHBOARD_SLUG = "offshore-energy-data-dashboard"

# The current-year page lives on the bare slug; earlier years are archived
# under "<slug>-<year>".
FIRST_ARCHIVE_YEAR = 2021

def _mongo_uri():
    """Same source order as Q:/Coding/infra lib_db.py - never hard-code the secret.

    env IRIS_MONGO_URI  ->  mongo_uri.txt next to this file (gitignored).
    On CI the DATABASE repository secret is mapped to IRIS_MONGO_URI.
    """
    uri = os.environ.get("IRIS_MONGO_URI")
    if uri:
        return uri.strip()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mongo_uri.txt")
    if os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    return None


MONGO_URI = _mongo_uri()
DATABASE_NAME = os.environ.get("IRIS_MONGO_DB", "dc_commodity")
COLLECTION_NAME = "ThiTruongKhoan"

# Both series live in one collection, in the same dataset/date/values shape
# that ThiTruongDien uses, keyed by a single ticker.
DATASET_WEEKLY = "weekly_jackup_rig_count"
DATASET_MONTHLY = "monthly_jackup_utilisation"
TICKER_NAME = "Global Rig Count"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS)}


# --------------------------------------------------------------------------
# fetching / html helpers
# --------------------------------------------------------------------------

def fetch(url, retries=3, timeout=60):
    """GET a page as text. Westwood 403s requests without a browser user agent."""
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except Exception as exc:  # noqa: BLE001 - network flakiness
            last = exc
        if attempt < retries:
            time.sleep(3 * attempt)
    raise RuntimeError("failed to fetch %s: %s" % (url, last))


def strip_noise(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    return html


def to_text(fragment):
    txt = re.sub(r"<(br|/p|/div|/tr|/h[1-6])[^>]*>", "\n", fragment, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html_mod.unescape(txt)
    txt = txt.replace(" ", " ").replace("�", "'")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return txt.strip()


def month_tabs(html):
    """Yield (month_name, year, inner_html) for each id="tab-<month>-<year>" panel."""
    html = strip_noise(html)
    marks = [
        (m.start(), m.group(1).lower(), int(m.group(2)))
        for m in re.finditer(r'<div[^>]*id="tab-([A-Za-z]+)-(20\d{2})"', html, re.I)
    ]
    for i, (start, month, year) in enumerate(marks):
        if month not in MONTH_NUM:
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(html)
        yield month, year, html[start:end]


def table_rows(fragment):
    """Return each <table> in the fragment as a list of cell-text rows."""
    tables = []
    for tbl in re.findall(r"<table.*?</table>", fragment, re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tbl, re.S | re.I):
            cells = [
                to_text(td).replace("\n", " ").strip()
                for td in re.findall(r"<t[dh].*?</t[dh]>", tr, re.S | re.I)
            ]
            if any(cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def to_number(value):
    if value is None:
        return None
    txt = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
    if txt in ("", "-", ".", "-."):
        return None
    try:
        num = float(txt)
    except ValueError:
        return None
    return int(num) if num.is_integer() else num


def shift_year(tab_month, month, year):
    """A month panel can spill one period into the neighbouring year."""
    if MONTH_NUM[tab_month] == 1 and month == 12:
        return year - 1
    if MONTH_NUM[tab_month] == 12 and month == 1:
        return year + 1
    return year


# --------------------------------------------------------------------------
# 1. weekly global offshore rig counts  ->  weekly jackup rig count
# --------------------------------------------------------------------------

DAY_RE = re.compile(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+)(?:\s+(20\d{2}))?", re.I)


def resolve_week_date(label, tab_month, tab_year):
    """'Monday 30th December 2024' inside the January-2025 panel -> 2024-12-30."""
    match = DAY_RE.search(label)
    if not match:
        return None
    month_name = match.group(2).lower()
    if month_name not in MONTH_NUM:
        return None
    day = int(match.group(1))
    month = MONTH_NUM[month_name]
    if match.group(3):
        year = int(match.group(3))
    else:
        year = shift_year(tab_month, month, tab_year)
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def jackup_column(header_cells):
    for i, cell in enumerate(header_cells):
        if "jackup" in cell.lower():
            return i
    return None


# Westwood only publishes the summary table once a month closes. Until then the
# newest weeks exist as chart images ("Week 34 ... TOTAL 371") whose numbers are
# not in the HTML, so those weeks are recorded as pending and filled in by a
# later run once the table appears.
CHART_CAPTION_RE = re.compile(r"week\s+(\d{1,2})\s+as\s+of\s+([^\n.]{6,44}?\d{4})", re.I)
JACKUP_IMG_RE = re.compile(
    r'<img[^>]*\bsrc="([^"]+)"[^>]*\balt="[^"]*Jackups[^"]*Week\s+(\d{1,2})[^"]*"', re.I
)
JACKUP_IMG_ALT_FIRST_RE = re.compile(
    r'<img[^>]*\balt="[^"]*Jackups[^"]*Week\s+(\d{1,2})[^"]*"[^>]*\bsrc="([^"]+)"', re.I
)


def jackup_chart_urls(fragment):
    """-> {week_number: image url} for the jackup charts in a month panel."""
    urls = {}
    for src, week in JACKUP_IMG_RE.findall(fragment):
        urls.setdefault(int(week), src)
    for week, src in JACKUP_IMG_ALT_FIRST_RE.findall(fragment):
        urls.setdefault(int(week), src)
    return urls


def parse_rig_counts(html, source_url):
    published, pending = {}, {}
    for month, year, fragment in month_tabs(html):
        tab = "%s-%s" % (month, year)

        for rows in table_rows(fragment):
            col = jackup_column(rows[0])
            if col is None:
                continue
            for cells in rows[1:]:
                if len(cells) <= col:
                    continue
                week_date = resolve_week_date(cells[0], month, year)
                jackup = to_number(cells[col])
                if week_date is None or jackup is None:
                    continue
                published[week_date] = {
                    "week_date": week_date,
                    "week_label": cells[0].strip(),
                    "jackup_rig_count": jackup,
                    "status": "published",
                    "source_tab": tab,
                    "source_url": source_url,
                }

        charts = jackup_chart_urls(fragment)
        for week_no, label in CHART_CAPTION_RE.findall(to_text(fragment)):
            week_date = resolve_week_date(label, month, year)
            if week_date is None:
                continue
            pending[week_date] = {
                "week_date": week_date,
                "week_label": label.strip(),
                "jackup_rig_count": None,
                "status": "chart_pending",
                "week_number": int(week_no),
                "chart_url": charts.get(int(week_no)),
                "source_tab": tab,
                "source_url": source_url,
            }

    for week_date, record in pending.items():
        published.setdefault(week_date, record)
    return [published[k] for k in sorted(published)]


# --------------------------------------------------------------------------
# 2. offshore energy data dashboard -> jackup utilisation + committed count
# --------------------------------------------------------------------------

# The wording drifts year to year: "The global committed jackup fleet remained at
# 377 units in June", "...count averaged 412 units in November", "...count
# decreased by six to 391 units in March".
ANCHOR_RE = re.compile(
    r"\b(?:global\s+|the\s+)?(?:committed\s+)?jackup\s*(?:\([^)]*\)\s*)?"
    r"(?:fleet|count|demand)\b",
    re.I,
)

# Fleet sizes are always written as "... N units"; month-on-month deltas are
# usually spelled out in words but occasionally appear as digits, so ignore any
# candidate below MIN_FLEET - the global jackup fleet has never been that small.
MIN_FLEET = 100
UNITS_RE = re.compile(r"(\d[\d,]*)\s*units\b", re.I)
COUNT_RE = re.compile(r"\b(?:to|at|averaged|totalled|totaled|reached)\s+([\d,]+)\b", re.I)

_LEAD = r"\b(?:to|at|were|was|of)\s+"
PAIR_STRICT_RE = re.compile(_LEAD + r"(\d{1,3})\s*%\s*,?\s+and\s+(\d{1,3})\s*%", re.I)
PAIR_LOOSE_RE = re.compile(r"(\d{1,3})\s*%\s*,?\s+and\s+(\d{1,3})\s*%", re.I)
SINGLE_RE = re.compile(_LEAD + r"(\d{1,3})\s*%", re.I)

IN_MONTH_RE = re.compile(r"\b(?:in|during|for)\s+(" + "|".join(MONTHS) + r")\b", re.I)


def split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def parse_utilisation_pair(sentence):
    """Extract (marketed committed %, total %) from one utilisation sentence.

    Handles the combined phrasing ("marketed committed utilisation and total
    utilisation at 86% and 78%") as well as the split phrasing ("marketed
    committed utilisation remained at 90% while total utilisation increased by
    2% to 83%").
    """
    pairs = PAIR_STRICT_RE.findall(sentence) or PAIR_LOOSE_RE.findall(sentence)
    if pairs:
        marketed, total = pairs[-1]
        return int(marketed), int(total)

    lowered = sentence.lower()
    idx = -1
    for token in ("total utilisation", "total utilization", "total fleet", "total "):
        idx = lowered.find(token)
        if idx != -1:
            break
    if idx == -1:
        return None, None

    left = SINGLE_RE.findall(sentence[:idx])
    right = SINGLE_RE.findall(sentence[idx:])
    if left and right:
        return int(left[-1]), int(right[-1])
    return None, None


def drilling_paragraphs(fragment):
    """Narrative paragraphs under the 'Offshore Drilling Rigs' heading of a panel."""
    text = to_text(fragment)
    idx = text.lower().find("offshore drilling rigs")
    if idx == -1:
        return []
    return [p.strip() for p in text[idx:].split("\n") if len(p.strip()) > 60]


def parse_jackup_paragraph(para):
    """-> dict with committed count, utilisation rates and the month narrated."""
    anchor_sentence = None
    for sentence in split_sentences(para):
        if ANCHOR_RE.search(sentence):
            anchor_sentence = sentence
            break
    if anchor_sentence is None:
        return None

    committed = None
    tail = anchor_sentence[ANCHOR_RE.search(anchor_sentence).end():]
    for raw in UNITS_RE.findall(tail):
        value = to_number(raw)
        if value is not None and value >= MIN_FLEET:
            committed = value
            break
    if committed is None:
        count_match = COUNT_RE.search(tail)
        if count_match:
            value = to_number(count_match.group(1))
            if value is not None and value >= MIN_FLEET:
                committed = value

    month_match = IN_MONTH_RE.search(anchor_sentence)

    marketed = total = None
    for sentence in split_sentences(para):
        if "utilis" not in sentence.lower() and "utiliz" not in sentence.lower():
            continue
        marketed, total = parse_utilisation_pair(sentence)
        if marketed is not None:
            break

    return {
        "committed_jackup_rig_count": committed,
        "marketed_committed_utilisation_pct": marketed,
        "total_utilisation_pct": total,
        "month_name": month_match.group(1).lower() if month_match else None,
    }


def parse_dashboard(html, source_url):
    """One record per month: committed jackup count + jackup utilisation rates."""
    records = {}

    def slot(year, month):
        key = (year, month)
        if key not in records:
            records[key] = {
                "data_month": "%04d-%02d" % (year, month),
                "month_date": datetime(year, month, 1, tzinfo=timezone.utc),
                "rig_type": "jackup",
                "source_url": source_url,
            }
        return records[key]

    for tab_month, tab_year, fragment in month_tabs(html):
        # ---- narrative: committed jackup count + utilisation rates ----------
        for para in drilling_paragraphs(fragment):
            if "jackup" not in para.lower():
                continue
            parsed = parse_jackup_paragraph(para)
            if parsed is None:
                continue

            # the panel for month N narrates the state of month N-1
            month_name = parsed["month_name"] or MONTHS[(MONTH_NUM[tab_month] - 2) % 12]
            month = MONTH_NUM[month_name]
            year = shift_year(tab_month, month, tab_year)

            doc = slot(year, month)
            for field in ("committed_jackup_rig_count",
                          "marketed_committed_utilisation_pct",
                          "total_utilisation_pct"):
                if parsed[field] is not None:
                    doc[field] = parsed[field]
            doc["source_tab"] = "%s-%s" % (tab_month, tab_year)
            break  # only the first jackup paragraph of a panel is the fleet summary

        # ---- contract backlog table (rig years, explicitly month-labelled) --
        for rows in table_rows(fragment):
            header = " ".join(rows[0]).lower()
            if "backlog" not in header:
                continue
            col = jackup_column(rows[0])
            if col is None:
                continue
            for cells in rows[1:]:
                match = re.match(r"([a-z]+)\s+(20\d{2})", cells[0].strip().lower())
                if not match or match.group(1) not in MONTH_NUM:
                    continue  # skips the "Difference" row
                if len(cells) <= col:
                    continue
                value = to_number(cells[col])
                if value is not None:
                    slot(int(match.group(2)),
                         MONTH_NUM[match.group(1)])["contract_backlog_rig_years"] = value

    return [records[k] for k in sorted(records)]


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def build_urls(slug, years, include_current):
    urls = []
    if include_current:
        urls.append("%s/%s" % (BASE, slug))
    for year in years:
        urls.append("%s/%s-%s" % (BASE, slug, year))
    return urls


def connect_mongo():
    from pymongo import MongoClient

    if not MONGO_URI:
        raise RuntimeError(
            "Thieu URI MongoDB. Dat bien moi truong IRIS_MONGO_URI, hoac tao file "
            "mongo_uri.txt (da gitignore) cung thu muc."
        )
    kwargs = {"serverSelectionTimeoutMS": 30000}
    try:
        import certifi

        kwargs["tlsCAFile"] = certifi.where()
    except ImportError:
        pass
    client = MongoClient(MONGO_URI, **kwargs)
    client.admin.command("ping")
    return client


WEEKLY_UNITS = {"Jackup Rig Count": "rigs"}
MONTHLY_UNITS = {
    "Committed Jackup Rig Count": "rigs",
    "Marketed Committed Utilisation": "%",
    "Total Utilisation": "%",
    "Contract Backlog": "rig years",
}


def _doc_id(dataset, date_text, ticker):
    """Deterministic _id so a re-scrape updates the row instead of adding one."""
    return hashlib.md5(("|".join((dataset, date_text, ticker))).encode("utf-8")).hexdigest()


def to_documents(weekly, monthly):
    """Fold both series into the shared ThiTruongKhoan document shape."""
    docs = []

    for rec in weekly:
        date_text = rec["week_date"].strftime("%Y-%m-%d")
        docs.append({
            "_id": _doc_id(DATASET_WEEKLY, date_text, TICKER_NAME),
            "dataset": DATASET_WEEKLY,
            "frequency": "weekly",
            "date": rec["week_date"],
            "date_text": date_text,
            "ticker": TICKER_NAME,
            "values": {"Jackup Rig Count": rec["jackup_rig_count"]},
            "units": WEEKLY_UNITS,
            "week_label": rec["week_label"],
            "source_url": rec["source_url"],
        })

    for rec in monthly:
        values = {}
        for name, field in (
            ("Committed Jackup Rig Count", "committed_jackup_rig_count"),
            ("Marketed Committed Utilisation", "marketed_committed_utilisation_pct"),
            ("Total Utilisation", "total_utilisation_pct"),
            ("Contract Backlog", "contract_backlog_rig_years"),
        ):
            if rec.get(field) is not None:
                values[name] = rec[field]
        docs.append({
            "_id": _doc_id(DATASET_MONTHLY, rec["data_month"], TICKER_NAME),
            "dataset": DATASET_MONTHLY,
            "frequency": "monthly",
            "date": rec["month_date"],
            "date_text": rec["data_month"],
            "ticker": TICKER_NAME,
            "values": values,
            "units": {k: v for k, v in MONTHLY_UNITS.items() if k in values},
            "source_url": rec["source_url"],
        })

    return docs


def upsert(collection, docs, key_fields):
    from pymongo import UpdateOne

    if not docs:
        return 0, 0
    now = datetime.now(timezone.utc)
    ops = []
    for doc in docs:
        payload = dict(doc)
        ops.append(
            UpdateOne(
                {k: doc[k] for k in key_fields},
                {"$set": payload, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        )
    result = collection.bulk_write(ops, ordered=False)
    return result.upserted_count, result.modified_count


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scrape Westwood offshore jackup rig data")
    parser.add_argument("--years", nargs="*", type=int, default=[],
                        help="archive years to scrape alongside the current page")
    parser.add_argument("--all", action="store_true",
                        help="scrape every archive year from %d" % FIRST_ARCHIVE_YEAR)
    parser.add_argument("--no-current", action="store_true",
                        help="skip the current-year (bare slug) page")
    parser.add_argument("--no-mongo", action="store_true",
                        help="parse only, do not write to MongoDB")
    parser.add_argument("--json", dest="json_path", help="dump parsed data to a JSON file")
    args = parser.parse_args()

    years = list(args.years)
    if args.all:
        years = list(range(FIRST_ARCHIVE_YEAR, datetime.now().year + 1))
    years = sorted(set(years))

    started = datetime.now(timezone.utc)
    print("=" * 70)
    print("Westwood offshore JACKUP scrape - %s UTC"
          % started.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)

    weekly, monthly = [], []

    print("\n[1/2] Weekly global offshore rig counts -> jackup weekly")
    for url in build_urls(RIG_COUNT_SLUG, years, not args.no_current):
        try:
            rows = parse_rig_counts(fetch(url), url)
        except urllib.error.HTTPError as exc:
            print("  - %s -> HTTP %s, skipped" % (url, exc.code))
            continue
        print("  - %s -> %d weekly rows" % (url, len(rows)))
        weekly.extend(rows)

    print("\n[2/2] Offshore energy data dashboard -> jackup utilisation")
    for url in build_urls(DASHBOARD_SLUG, years, not args.no_current):
        try:
            rows = parse_dashboard(fetch(url), url)
        except urllib.error.HTTPError as exc:
            print("  - %s -> HTTP %s, skipped" % (url, exc.code))
            continue
        print("  - %s -> %d monthly rows" % (url, len(rows)))
        monthly.extend(rows)

    if not weekly and not monthly:
        raise RuntimeError("no data parsed from either page - the layout may have changed")

    # de-duplicate across pages (archives restate boundary periods); a published
    # table row always wins over a still-pending chart week
    by_week = {}
    for rec in weekly:
        seen = by_week.get(rec["week_date"])
        if seen is None or (seen["jackup_rig_count"] is None
                            and rec["jackup_rig_count"] is not None):
            by_week[rec["week_date"]] = rec
    weekly = [by_week[k] for k in sorted(by_week)]
    merged = {}
    for rec in monthly:
        merged.setdefault(rec["data_month"], {}).update(rec)
    monthly = [merged[k] for k in sorted(merged)]

    pending = [r for r in weekly if r["jackup_rig_count"] is None]
    print("\nParsed %d weekly jackup rows (%d awaiting Westwood's monthly table), "
          "%d monthly jackup rows" % (len(weekly), len(pending), len(monthly)))
    for rec in weekly[-4:]:
        print("  week  %s  jackup rig count = %s%s"
              % (rec["week_date"].strftime("%Y-%m-%d"),
                 rec["jackup_rig_count"],
                 "  (chart only, number not in HTML yet)"
                 if rec["jackup_rig_count"] is None else ""))
    for rec in monthly[-3:]:
        print("  month %s  committed=%s  marketed_committed_util=%s%%  total_util=%s%%  backlog=%s"
              % (rec["data_month"], rec.get("committed_jackup_rig_count"),
                 rec.get("marketed_committed_utilisation_pct"),
                 rec.get("total_utilisation_pct"),
                 rec.get("contract_backlog_rig_years")))

    documents = to_documents(weekly, monthly)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(documents, handle, indent=2, default=str)
        print("  JSON written: %s" % args.json_path)

    if args.no_mongo:
        print("\n--no-mongo set, nothing written to MongoDB.")
        return 0

    client = connect_mongo()
    print("\nConnected to MongoDB (%s)" % DATABASE_NAME)
    try:
        collection = client[DATABASE_NAME][COLLECTION_NAME]
        collection.create_index([("ticker", 1), ("dataset", 1), ("date", 1)])
        collection.create_index("date")

        ins, mod = upsert(collection, documents, ["_id"])
        print("  %s: %d inserted, %d updated (%d weekly + %d monthly)"
              % (COLLECTION_NAME, ins, mod, len(weekly), len(monthly)))
    finally:
        client.close()

    print("\nDone in %.1fs" % (datetime.now(timezone.utc) - started).total_seconds())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - the scheduled task needs a clean exit code
        print("\nSCRAPE FAILED: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        sys.exit(1)
