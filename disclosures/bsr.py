"""
BSR (Binh Son Refining / Loc hoa dau Viet Nam) - one quarter from its own disclosures.

Documents:

1. The quarterly consolidated BCTC ("Bao cao tai chinh hop nhat Quy X nam Y", not the
   "rieng" / "giai trinh" / reviewed half-year ones) from CafeF or Vietstock, whichever
   downloads first (common.fetch_bctc). Feeds:
     Net revenue, Gross profit, <product> net revenue, <product> gross profit.
   Where: notes 22 "DOANH THU BAN HANG VA CUNG CAP DICH VU" (a) revenue by product)
   and 23 "GIA VON HANG BAN" (COGS by product, same rows + provision), usually the
   3rd-5th page from the end. Rows are read by LABEL (the product list changes from
   quarter to quarter: RON83, Xang nen, Jet A-1K, DO L-62, SAF, Propylene come and go):
     DO     = Nhien lieu diezen (DO 0,05S) + DO L-62
     A95    = Xang khong chi RON 95 + E10/E5 RON 95
     A92    = Xang khong chi RON 92 + E5 RON 92
     JetA1  = Jet A-1 + Jet A-1K
     LPG    = Khi dau mo hoa long (LPG)
     Polypropylene = PP grades (T3034, T3045, TF4035, F3030, I3110) + Propylene
     FO     = Fuel oil
     Others = total - the above (KO, sulphur, RON83, xang nen, SAF, RFCC naphtha,
              port services, other, and on the COGS side the inventory provision)
   Gross profit = revenue - COGS per product; total GP = note 22 total - note 23 total
   (= P&L lines 20; BSR has no revenue deductions, so note 22 total = net revenue).
   YTD: the note columns are "Nam nay / Nam truoc" = year to date, so for Q2-Q4 the
   quarter = this report's note - the previous quarterly report's note.
   OCR: from 2Q2026 the BCTC is a scan (only the eoffice stamp has text); the 1Q2026
   one has a text layer but the Vietnamese labels are lost (broken font). So the note
   pages are always OCR'd (RapidOCR, ~2-4 min per page on this machine, cached as JSON
   in downloads/BSR). Numbers are assigned to the "Nam nay" column by x position: the
   printed prior-year column is sometimes shifted a row (2Q2026) and OCR drops lone "0"s.

2. "BAN TIN NHA DAU TU BSR QUY X NAM Y" (investor bulletin, ~1-5 weeks after the BCTC), from
   the bsr.com.vn IR tables (TinNganhDien, which indexes the same site, as a fallback).
   Feeds: Total Volume (k tons) = "San luong tieu thu", Production (k tons) = "San luong
   san xuat", Utilisation (%) = "van hanh o muc X% cong suat".
   Where: the results table page ("KH 20yy ... TH/KH", usually page 8): rows production,
   sales, net revenue, PBT, PAT with columns [last year, this year, %, plan, % plan].
   Units: Q1 issues in "nghin tan", the others in "trieu tan" (2-3 decimals -> +-5 kt).
   YTD: Q2/Q3/Q4 issues are cumulative (1H / 9M / 12M) -> quarter = this - previous issue.
   The bulletin revenue is checked against the BCTC YTD revenue to confirm the period.
   Utilisation is only stated in some issues and for the cumulative period; Q2-Q4 are
   derived day-weighted from the two cumulative figures (REVIEW).
   The 3Q2025 issue is a scan -> OCR of pages 7-9.

Not sourceable from disclosures (left out): product volumes (bulletins only give a
revenue-mix pie in %), Asian 2-1-1 crack spread and Brent (charts only, no numbers).

Caveats seen when checking against the Diamond history (CSV):
- 1Q2026 Polypropylene in the CSV is T3034 only (923.78); the 2Q2026 CSV figure implies
  all grades + propylene (1H 2,519.6 - 923.78). Here all grades are used for every
  quarter, so 1Q/2Q PP and Others differ from the CSV by the minor grades.
- 2Q2026 CSV gross profit (8,797.6) is not the quarterly report's (P&L line 20 = 8,606.3;
  notes give the same); the gap sits in Others GP. Possibly taken from the reviewed 1H
  report (a scan, not checked).
- 2Q2026 CSV Total Volume 1,853 vs bulletin 1H 4,028 - 1Q 1,997 = 2,031.
- Production in the CSV matched the bulletins in 1Q/2Q2025 but not 3Q2025 (1,685.6 vs
  9M 5.87 Mt - 1H 3.848 Mt = ~2,022).
- The 2Q2026 bulletin says "123,5% cong suat" for 1H (same as 1Q) but also "(124%)" -
  the derived 2Q utilisation (123.5) is soft.
"""

import calendar
import json
import os
import re
import unicodedata
from datetime import date
from urllib.parse import urljoin

from disclosures.common import Result, download, fetch, fetch_bctc, find_documents, label, prev_quarter, to_bn

TICKER = "BSR"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(HERE, "company_bsr_quarterly.csv")

PRODUCTS = ["DO", "A95", "A92", "JetA1", "LPG", "Polypropylene", "FO"]  # + Others = rest

# normalised (no accents, lower case) row label -> product
ROW_MAP = [
    ("LPG", r"lpg|khi dau mo hoa long"),
    ("A95", r"ron\s*95"),
    ("A92", r"ron\s*92"),
    ("JetA1", r"jet\s*a"),
    ("DO", r"\(do\b|\bdo\s*0|\bdo\)|dieze?n|diesel"),
    ("FO", r"fuel\s*oil"),
    ("Polypropylene", r"^[a-z1i]{1,2}\d{3,4}$|polypropylen|^propylen|\(pp\)"),
]

NUMTOK = re.compile(r"^\(?-?\d{1,3}(?:\.\d{3})+\)?$|^\(?-?\d+\)?$")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def plain(text):
    """'Xăng không chì RON 95' -> 'xang khong chi ron 95' (OCR output is often unaccented)."""
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    return re.sub(r"\s+", " ", "".join(c for c in text if not unicodedata.combining(c))).strip().lower()


def vnd(token):
    """'1.234.567' / '(1.234)' -> number (VND, dots as thousands)."""
    s = token.strip()
    neg = s.startswith("(") or s.startswith("-")
    s = s.strip("()-").replace(".", "")
    return -float(s) if neg else float(s)


def days(q, y):
    return sum(calendar.monthrange(y, m)[1] for m in range(3 * q - 2, 3 * q + 1))


def ytd_days(q, y):
    return (date(y, 3 * q, calendar.monthrange(y, 3 * q)[1]) - date(y, 1, 1)).days + 1


def csv_columns():
    with open(CSV, encoding="utf-8") as handle:
        return handle.readline().strip().split(",")


def page_texts(path):
    """{page: text layer} via PyMuPDF (pdfplumber stalls on the scanned BCTC)."""
    import fitz

    doc = fitz.open(path)
    try:
        return {i + 1: page.get_text() for i, page in enumerate(doc)}
    finally:
        doc.close()


_OCR = None


def ocr_boxes(path, page, zoom=2.5):
    """RapidOCR boxes of one page [(x0, x1, y centre, height, text)], cached next to the PDF.
    (common.ocr_pages joins boxes into text lines and loses x positions, which the column
    assignment below needs.)"""
    global _OCR
    cache = f"{os.path.splitext(path)[0]}.p{page}.ocr.json"
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as handle:
            return [tuple(b) for b in json.load(handle)]
    import fitz
    from rapidocr_onnxruntime import RapidOCR

    _OCR = _OCR or RapidOCR()
    doc = fitz.open(path)
    try:
        png = doc[page - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
    finally:
        doc.close()
    boxes, _ = _OCR(png)
    out = []
    for b, text, _score in boxes or []:
        xs, ys = [p[0] for p in b], [p[1] for p in b]
        out.append((min(xs), max(xs), sum(ys) / 4, max(ys) - min(ys), text))
    with open(cache, "w", encoding="utf-8") as handle:
        json.dump(out, handle, ensure_ascii=False)
    return out


def ocr_lines(boxes):
    """OCR boxes grouped into visual lines, top to bottom, each sorted left to right."""
    if not boxes:
        return []
    heights = sorted(b[3] for b in boxes)
    tol = heights[len(heights) // 2] * 0.5
    lines, cur, cy = [], [], None
    for b in sorted(boxes, key=lambda b: b[2]):
        if cy is not None and abs(b[2] - cy) > tol:
            lines.append(sorted(cur, key=lambda b: b[0]))
            cur = []
        cur.append(b)
        cy = sum(x[2] for x in cur) / len(cur)
    lines.append(sorted(cur, key=lambda b: b[0]))
    return lines


def ocr_rows(boxes):
    """Boxes -> rows [{label, nums: [(x1, value)]}] top to bottom."""
    out = []
    label_zone = max((b[1] for b in boxes), default=0) * 0.45  # grade codes like 'I3110' OCR'd as 13110
    for line in ocr_lines(boxes):
        label_parts, nums = [], []
        for _x0, x1, _y, _h, text in line:
            t = text.replace(" ", "")
            if NUMTOK.match(t) and x1 > label_zone:
                nums.append((x1, vnd(t)))
            else:
                label_parts.append(text)
        out.append({"label": " ".join(label_parts).strip(), "nums": nums})
    return out


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

def quarter_regex(q, y):
    """Title names quarter q of year y ('Quy 2 nam 2026', 'QUY 1 2026', 'Q4/2025', 'Quy II')."""
    roman = {1: "i", 2: "ii", 3: "iii", 4: "iv"}[q]
    return rf"(?:\bquy\s*0?{q}\b|\bquy\s*{roman}\b|\bq\s*0?{q}\b|\b{q}q)[^0-9]{{0,20}}{y}"


IR_PAGES = ["https://bsr.com.vn/bao-cao", "https://bsr.com.vn/tai-lieu-danh-cho-nha-dau-tu"]


def ir_list():
    """The bsr.com.vn IR tables
    (rows tr.document-item, title in p.document-title, all rows in one response).
    common.company_page does not fit - the link text is 'Tai ve', not the title."""
    out = []
    for page in IR_PAGES:
        try:
            html = fetch(page).decode("utf-8", "replace")
        except Exception:
            continue
        for row in re.findall(r'<tr[^>]*document-item.*?</tr>', html, flags=re.S | re.I):
            title = re.search(r'document-title[^>]*>(.*?)</p>', row, flags=re.S)
            href = re.search(r'<a[^>]+href="(/c/document_library/get_file[^"]+)"', row)  # the "Tai ve" link
            if title and href:
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title.group(1))).strip()
                out.append({"title": text, "url": urljoin(page, href.group(1).replace("&amp;", "&"))})
    return out


def pick(kind, q, y):
    """Newest document of `kind` ("bulletin"; "bctc" only as a last resort) for quarter q, year y."""
    broad = r"tai chinh hop nhat" if kind == "bctc" else r"ban tin"
    query = r"t[àa]i ch[íi]nh h[ợo]p nh[ấa]t|tai chinh hop nhat" if kind == "bctc" else r"b[ảa]n tin|ban tin"
    for source in ("site", "index"):  # the company website first, TinNganhDien if it misses one
        docs = ir_list() if source == "site" else find_documents(
            TICKER, query, since=f"{y}-01-01", types=("document",), limit=60)
        for d in docs:
            if not re.search(broad, plain(d["title"])):
                continue
            t = plain(d["title"])
            if kind == "bctc" and re.search(r"rieng|giai trinh|soat xet|giua nien do|kiem toan", t):
                continue
            if re.search(quarter_regex(q, y), t):
                return d
            if kind == "bulletin" and q == 4 and re.search(rf"nam\s*{y}\b", t) \
                    and not re.search(r"\bquy\b|\bq\s*\d", t):
                return d  # 4Q issue titled "Ban tin ... Nam 2024"
    return None


# --------------------------------------------------------------------------
# BCTC notes 22 / 23
# --------------------------------------------------------------------------

def note_pages(path):
    """Candidate pages for notes 22/23, most likely first."""
    texts = page_texts(path)
    n = len(texts)
    hits = [p for p, t in texts.items()
            if re.search(r"DOANH THU B.N H.NG V. CUNG C", t) and re.search(r"FUEL OIL|RFCC|T3034", t)]
    if hits:
        return [hits[0], hits[0] + 1]
    order = [n - 4, n - 3, n - 5, n - 2, n - 6, n - 1, n - 7, n - 8, n - 9, n - 10]
    return [p for p in order if 3 <= p <= n]


def parse_table(rows, start, heading):
    """Rows of the table under `heading` -> ({label: this-year VND}, total). Columns by x."""
    i = next((k for k in range(start, len(rows))
              if re.search(heading, plain(rows[k]["label"])) and not rows[k]["nums"]), None)
    if i is None:
        return None, None, start
    # 'Nam nay' column = numbers right-aligned under the header, prior year further right:
    # split the VND amounts' right edges at their widest gap (page numbers etc. ignored)
    xs = sorted(x for r in rows[i:i + 40] for x, v in r["nums"] if abs(v) >= 1e6)
    if len(xs) < 2:
        return None, None, start
    gap, split = max((b - a, (a + b) / 2) for a, b in zip(xs, xs[1:]))
    if gap < (xs[-1] - xs[0]) * 0.3:
        split = None  # one column only
    lines, total = {}, None
    for k in range(i + 1, len(rows)):
        lab = plain(rows[k]["label"])
        this = [v for x, v in rows[k]["nums"] if (split is None or x < split) and (abs(v) >= 1e6 or v == 0)]
        if re.fullmatch(r"(tong )?cong", lab) and this:
            return lines, this[0], k + 1
        if re.fullmatch(r"(vnd\s*)*|nam nay.*|ky nay.*|a\) doanh thu", lab):
            if not lab and this and lines:
                total = this[0]
                return lines, total, k + 1
            continue
        if not re.search(r"[a-z]", lab) and not re.fullmatch(r"\d{4,5}", lab):  # keep 'I3110' read as 13110
            continue
        lines[rows[k]["label"]] = this[0] if this else 0.0  # lone '0' is often not OCR'd
    return lines, total, len(rows)


def read_notes(path):
    """{'rev': {label: VND}, 'rev_total', 'cogs': {...}, 'cogs_total'} YTD from notes 22/23."""
    for p in note_pages(path):
        rows = ocr_rows(ocr_boxes(path, p))
        if not any(re.search(r"doanh thu ban hang va cung cap", plain(r["label"])) and not r["nums"]
                   for r in rows):
            continue
        for extra in (p + 1, p + 2):  # note 23 is on the same or the next page
            r_lines, r_total, k = parse_table(rows, 0, r"doanh thu ban hang va cung cap")
            if r_total:
                c_lines, c_total, _ = parse_table(rows, k, r"gia von hang ban")
                if c_total:
                    return {"rev": r_lines, "rev_total": r_total, "cogs": c_lines, "cogs_total": c_total}
            rows += ocr_rows(ocr_boxes(path, extra))
        return None
    return None


def by_product(lines, total):
    out = dict.fromkeys(PRODUCTS, 0.0)
    for lab, v in lines.items():
        t = plain(lab)
        for prod, pattern in ROW_MAP:
            if re.search(pattern, t):
                out[prod] += v
                break
    out["Others"] = total - sum(out[p] for p in PRODUCTS)
    return out


def bctc_ytd(q, y, res):
    """YTD revenue / COGS by product (VND) from the quarterly BCTC of q, y."""
    path, doc = fetch_bctc(TICKER, q, y)
    if not path:
        return None
    notes = read_notes(path)
    if not notes:
        res.warn(f"{label(q, y)} BCTC: notes 22/23 not found ({doc['url']})")
        return None
    res.sources.append(f"{doc['title']} ({doc['site']}) {doc['url']}")
    for side in ("rev", "cogs"):
        s = sum(notes[side].values())
        if abs(s - notes[side + "_total"]) > abs(notes[side + "_total"]) * 0.001:
            res.warn(f"{label(q, y)} note {'22' if side == 'rev' else '23'}: rows sum {to_bn(s):,.1f} "
                     f"vs total {to_bn(notes[side + '_total']):,.1f} bn - OCR misread? (Others absorbs it)")
    unmapped = [lab for lab in notes["rev"] if not any(re.search(p, plain(lab)) for _, p in ROW_MAP)]
    if unmapped:
        res.warn(f"{label(q, y)} rows put in Others: {', '.join(unmapped)}")
    return {"rev": by_product(notes["rev"], notes["rev_total"]),
            "cogs": by_product(notes["cogs"], notes["cogs_total"]),
            "rev_total": notes["rev_total"], "cogs_total": notes["cogs_total"]}


# --------------------------------------------------------------------------
# investor bulletin
# --------------------------------------------------------------------------

NUM = r"\d{1,3}(?:[.,]\d{1,3})*"
ROW = re.compile(rf"({NUM})\s+({NUM})\s+\S{{0,2}}?\s*-?\d[\d.,]*\s*%\s+({NUM})")


def bulletin(q, y, res):
    """{'sales', 'production' (kt), 'revenue' (bn), 'util' (%) or None} as printed (maybe cumulative)."""
    doc = pick("bulletin", q, y)
    if not doc:
        return None
    path = download(doc["url"], TICKER)
    texts = page_texts(path)
    pages = [p for p, t in texts.items() if re.search(r"TH\s*/\s*KH", t)]
    if not pages and sum(len(t) for t in texts.values()) < 2000:  # scanned issue (3Q2025)
        for p in (8, 7, 9):
            if p in texts:
                texts[p] = "\n".join(" ".join(b[4] for b in line) for line in ocr_lines(ocr_boxes(path, p)))
                if re.search(r"TH\s*/\s*KH", texts[p]):
                    pages = [p]
                    break
    for p in pages:
        flat = re.sub(r"\s+", " ", texts[p])
        found = ROW.findall(flat)
        if len(found) < 3:
            continue
        mt = re.search(r"tri[eệ]u t[aấ]n", plain(flat))
        kt = re.search(r"ngh[iì]n t[aấ]n", plain(flat))

        def tons(s):
            if mt and not kt:
                return float(s.replace(".", "").replace(",", ".")) * 1000
            return float(s.replace(".", "").replace(",", ""))
        prod, sales, rev = found[0][1], found[1][1], found[2][1]
        out = {"production": tons(prod), "sales": tons(sales), "revenue": vnd(rev),
               "coarse": bool(mt and not kt) and not re.search(r"[.,]\d{3}$", sales),
               "util": None, "url": doc["url"]}
        whole = plain(" ".join(texts.values()))
        m = (re.search(r"o\s*muc\s*(\d{2,3}(?:,\d+)?)\s*%\s*cong suat", whole)
             or re.search(r"(\d{2,3}(?:,\d+)?)\s*%\s*cong suat", whole)
             or re.search(r"cong suat[^%]{0,30}?\(?(\d{2,3}(?:,\d+)?)\s*%", whole))
        if m:
            out["util"] = float(m.group(1).replace(",", "."))
            out["util_approx"] = bool(re.search(r"(hon|khoang|gan)\s*$", whole[:m.start(1)][-12:]))
        res.sources.append(doc["url"])
        return out
    res.warn(f"{label(q, y)} bulletin: results table not found ({doc['url']})")
    return None


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

def collect(q, y):
    res = Result()
    cols = set(csv_columns())
    tag = label(q, y)
    pq, py = prev_quarter(q, y)
    vals = {}

    # 1) consolidated BCTC: revenue / GP by product
    cur = bctc_ytd(q, y, res)
    ytd_rev = cur["rev_total"] if cur else None
    if cur is None:
        res.warn(f"{tag}: BCTC hop nhat quy chua co tren CafeF/Vietstock (thuong ~30 ngay sau quy)")
    else:
        prev = None
        if q > 1:
            prev = bctc_ytd(pq, py, res)
            if prev is None:
                res.warn(f"{tag}: previous quarter's BCTC missing - cannot de-cumulate the YTD notes")
        if q == 1 or prev:
            def qv(side, key):
                return cur[side][key] - (prev[side][key] if prev else 0.0)
            rev_total = cur["rev_total"] - (prev["rev_total"] if prev else 0.0)
            cogs_total = cur["cogs_total"] - (prev["cogs_total"] if prev else 0.0)
            vals["Net revenue (VNDbn)"] = to_bn(rev_total)
            vals["Gross profit (VNDbn)"] = to_bn(rev_total - cogs_total)
            for p in PRODUCTS + ["Others"]:
                vals[f"{p} net revenue (VNDbn)"] = to_bn(qv("rev", p))
                vals[f"{p} gross profit (VNDbn)"] = to_bn(qv("rev", p) - qv("cogs", p))
            if q > 1:
                res.warn(f"{tag}: notes are year-to-date -> quarter = {tag} note - {label(pq, py)} note")
            # check: product split adds up (Others is the balancing item, so look at its sign/size)
            if vals["Others net revenue (VNDbn)"] < 0 or vals["Others net revenue (VNDbn)"] > 0.1 * vals["Net revenue (VNDbn)"]:
                res.warn(f"{tag}: Others revenue {vals['Others net revenue (VNDbn)']:,.1f} bn looks off - check note rows")

    # 2) investor bulletin: sales volume, production, utilisation
    b = bulletin(q, y, res)
    if b is None:
        res.warn(f"{tag}: investor bulletin not published yet (usually 1-5 weeks after the BCTC)")
    else:
        cumulative = q > 1
        if ytd_rev:  # which period does the bulletin cover? compare its revenue with the BCTC
            if abs(b["revenue"] - to_bn(ytd_rev)) <= to_bn(ytd_rev) * 0.01:
                cumulative = q > 1
            elif q > 1 and "Net revenue (VNDbn)" in vals and \
                    abs(b["revenue"] - vals["Net revenue (VNDbn)"]) <= vals["Net revenue (VNDbn)"] * 0.01:
                cumulative = False
            else:
                res.warn(f"{tag} bulletin revenue {b['revenue']:,.0f} bn matches neither the BCTC YTD "
                         f"({to_bn(ytd_rev):,.0f}) nor the quarter - check the period")
        pb = bulletin(pq, py, res) if cumulative else None
        if cumulative and pb is None:
            res.warn(f"{tag}: bulletin is cumulative and {label(pq, py)} bulletin is missing - volumes left out")
        else:
            sales = b["sales"] - (pb["sales"] if pb else 0.0)
            prod = b["production"] - (pb["production"] if pb else 0.0)
            vals["Total Volume (k tons)"] = sales
            vals["Production (k tons)"] = prod
            if cumulative:
                res.warn(f"{tag}: bulletin is cumulative -> volumes = {tag} YTD - {label(pq, py)} YTD")
            if b["coarse"] or (pb and pb["coarse"]):
                res.warn(f"{tag}: bulletin volumes printed in million tons (2-3 decimals) -> +-5-10 kt")
            for name, v in (("sales", sales), ("production", prod)):
                if not 1000 <= v <= 3000:
                    res.warn(f"{tag}: quarterly {name} {v:,.0f} kt outside 1,000-3,000 - check the bulletin")
            # utilisation
            if b["util"] is not None and (not cumulative or (pb and pb["util"] is not None)):
                u = b["util"]
                if cumulative:
                    u = (b["util"] * ytd_days(q, y) - pb["util"] * ytd_days(pq, py)) / days(q, y)
                    res.warn(f"{tag}: utilisation derived day-weighted from YTD {b['util']}% and "
                             f"{label(pq, py)} YTD {pb['util']}%")
                if b.get("util_approx") or (pb and pb.get("util_approx")):
                    res.warn(f"{tag}: utilisation stated as 'hon/khoang X%' - approximate")
                vals["Utilisation (%)"] = u
            elif b["util"] is None:
                res.warn(f"{tag}: bulletin does not state utilisation")
            else:
                res.warn(f"{tag}: utilisation only stated for the cumulative period - not de-cumulated")

    res.values = {k: round(v, 2) for k, v in vals.items() if k in cols and v is not None}
    return res
