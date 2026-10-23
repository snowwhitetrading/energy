"""
Petrolimex (PLX) - quarter figures from the quarterly consolidated financial statements.

Document: the quarterly consolidated BCTC from CafeF, Vietstock or petrolimex.com.vn (its IR
list is filled from a CMS search API, `_reports()`), whichever downloads first (common.fetch_bctc).
Published ~30 days after quarter end (Q1 ~4 May, Q2 ~30 Jul, Q3 ~30 Oct, Q4 ~30 Jan), together
with the parent-only report ("Công ty mẹ", skipped) and a "Giải trình" letter (profit only).
The reviewed half-year ("6 tháng ... sau soát xét", late Aug) and audited FY ("năm ... sau kiểm
toán", late Mar) are NOT used: the quarterly report has a this-quarter P&L column.

The PDF is a scan (no text layer) -> OCR (common.ocr_pages, RapidOCR), two pages of ~36:

- P&L "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH HỢP NHẤT" (page 5). Columns: Quý X năm nay, năm
  trước, lũy kế năm nay, năm trước - first amount on the line = this quarter (VND -> VNDbn):
      code 10 DT thuần          -> Net revenue (VNDbn)
      code 20 LN gộp            -> Gross profit (VNDbn)
  Checked: 10 - 11 (giá vốn) = 20.
- "Thuyết minh báo cáo chi tiết kết quả kinh doanh giữa các niên độ" (2nd-3rd last page),
  item 3 Giá vốn hàng bán, code F35 "Dự phòng giảm giá HTK" -> Inventory provision (VNDbn)
  (positive = charge, negative = reversal). This note is YEAR-TO-DATE only, so the quarter =
  this report's F35 - previous quarter's report F35 (Q1: as is). Rows F30..F36 must add up
  to F3A (total COGS) in both columns - that resolves which column a lone amount belongs to
  (blank cells are common, e.g. F36 has no prior-year figure) and catches OCR digit errors.
  Checked against the Diamond history: 1Q2026 6,321.70; 2Q2026 248.00 - 6,321.70 = -6,073.70.

Not sourceable from PLX disclosures (left out):
- Domestic Volume (k m3): not in the quarterly FS, the Giải trình letter (only "+12% YoY") or
  the press releases; the annual "Báo cáo đánh giá kết quả SXKD" gives full-year volume only.
  Unit gross profit (VND/litre) = GP / volume * 1000 therefore needs the analyst's volume: it
  is computed only when the CSV already holds Domestic Volume for the quarter (flagged).
- Segment split: the quarterly FS has no segment note (the reviewed 6M / audited FY reports do -
  petroleum / petrochemicals / transport / others - which only gives H1 and H2, not quarters).

Caveats: OCR mixes '.' and ',' as thousand separators ("1.086.055,224.203") -> _amount() treats
every 3-digit group as thousands (all amounts are whole VND). OCR text per page is cached next
to the PDF (common.ocr_pages).
"""

import base64
import itertools
import json
import re

from disclosures.common import (UA, Result, fetch_bctc, page_text, pdf_pages, period_words,
                                prev_quarter, to_bn)

TICKER = "PLX"
NR, GP = "Net revenue (VNDbn)", "Gross profit (VNDbn)"
VOL, UNIT_GP, PROV = "Domestic Volume (k m3)", "Unit gross profit (VND/litre)", "Inventory provision (VNDbn)"

# CMS search API behind petrolimex.com.vn/bao-cao-nha-dau-tu.html (ids from the site's all.js)
API = "https://portals.petrolimex.com.vn/~apis/portals/cms.item/search"
FILTER = {"And": [{"SystemID": {"Equals": "6783dc1271ff449e95b74a9520964169"}},
                  {"RepositoryID": {"Equals": "a95451e23b474fe5886bfb7cf843f53c"}},
                  {"RepositoryEntityID": {"Equals": "81ec1fdd97264b8e9f50d9fbdb8b8dcc"}},  # vi
                  {"Status": {"Equals": "Published"}}]}
AMOUNT = r"\(?-?\d{1,3}(?:[.,]\s?\d{3})+\)?"
NOT_QUARTERLY = r"c[ôo]ng\s*ty\s*m[ẹe]|ri[êe]ng|6\s*th[áa]ng|6T|so[áa]t\s*x[ée]t|ki[ểe]m\s*to[áa]n|gi[ữu]a\s*ni[êe]n"


def _amount(text):
    """'1.086.055,224.203' / '(1.427.432.061.892)' -> int."""
    neg = text.startswith("(") or text.startswith("-")
    v = int(re.sub(r"\D", "", text))
    return -v if neg else v


def _amounts(line):
    return [_amount(m) for m in re.findall(AMOUNT, line)]


def _ocr(path, page):
    return page_text(path, page)  # text layer, else full-resolution OCR (cached)


# --------------------------------------------------------------------------
# finding the report
# --------------------------------------------------------------------------

def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def _reports(pages=3):
    """[{title, url, date}] of the IR "Báo cáo tài chính" tab, newest first."""
    import requests

    out = []
    for n in range(1, pages + 1):
        req = {"FilterBy": FILTER, "SortBy": {"Created": "Descending"},
               "Pagination": {"TotalRecords": -1, "TotalPages": 0, "PageSize": 100, "PageNumber": n}}
        params = {"x-request": _b64(req), "ShowAttachments": "x", "x-device-id": "energy-disclosures",
                  "x-app-name": base64.urlsafe_b64encode(b"NGX Websites").decode().rstrip("="),
                  "x-app-platform": base64.urlsafe_b64encode(b"Desktop PWA").decode().rstrip("="),
                  "language": "vi-VN"}
        r = requests.get(API, params=params, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        data = r.json()
        for o in data.get("Objects") or []:
            if o.get("DocumentType") != "Báo cáo tài chính":
                continue
            url = o.get("DocumentURL") or ((o.get("Attachments") or [{}])[0].get("URIs") or {}).get("Direct")
            if url:
                out.append({"title": o.get("Title", ""), "url": url, "date": o.get("AnnouncedDate", "")[:10]})
        if n >= (data.get("Pagination") or {}).get("TotalPages", 0):
            break
    return out


def _website(q, y):
    """The quarter's consolidated BCTC on petrolimex.com.vn/bao-cao-nha-dau-tu.html (CMS API) as
    [{title, kind, url}] - one of the three sources common.fetch_bctc races."""
    pattern = r"(?:BCTC|b[áa]o\s*c[áa]o\s*t[àa]i\s*ch[íi]nh)\s*(?:h[ợo]p\s*nh[ấa]t|HN).*" + period_words(q, y)
    return [{"title": d["title"], "kind": "quarter", "url": d["url"]} for d in _reports()
            if re.search(pattern, d["title"], re.I) and not re.search(NOT_QUARTERLY, d["title"], re.I)]


def _report(q, y, res):
    """(local path, source label) of the quarter's consolidated BCTC from CafeF, Vietstock or
    petrolimex.com.vn, whichever downloads first. (None, None) when it is not out yet."""
    path, doc = fetch_bctc(TICKER, q, y, website=_website)
    return (path, f"{doc['title']} ({doc['site']}) {doc['url']}") if path else (None, None)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _pnl(path, res):
    """{code: this-quarter VND} for codes 10, 11, 20 from the consolidated P&L page."""
    for page in (5, 4, 6, 3, 7):
        text = _ocr(path, page)
        if not re.search(r"K[EÉ]T\s*QU[AẢ]\s*HO[AẠ]T\s*[DĐ][OỘQ]NG\s*KINH\s*DOANH", text, re.I):
            continue
        out = {}
        for line in text.splitlines():
            # B02 item number (3. / 4. / 5.) or the line code - OCR sometimes drops one of them
            m = re.match(r"\s*0?([345])\s*[.,]", line)
            code = {"3": "10", "4": "11", "5": "20"}[m.group(1)] if m else None
            if not code:
                m = re.search(r"\)\s*(10|20)\s|\s(11)\s+VI", line)
                code = m and (m.group(1) or m.group(2))
            nums = _amounts(line)
            if code and code not in out and nums:
                out[code] = nums[0]
        if out:
            return page, out
    res.warn("P&L page not found (pages 3-7) - layout changed?")
    return None, {}


def _cogs_note(path, res):
    """YTD (this year, last year) of F35 inventory provision, from the P&L detail note."""
    n = pdf_pages(path)
    for page in (n - 2, n - 1, n - 3, n, n - 4):
        text = _ocr(path, page)
        if not re.search(r"\bF3A\b", text):
            continue
        rows = {}
        for line in text.splitlines():
            m = re.search(r"\b(F3[0-6A])\b", line)
            if m and m.group(1) not in rows:
                rows[m.group(1)] = [v for v in _amounts(line[m.end():])]
        total = rows.pop("F3A", [])
        if len(total) != 2:
            res.warn(f"p{page}: total COGS F3A not read as 2 columns ({total})")
            return page, None
        # each row: 2 amounts = (this, last); 1 amount = one of the columns blank; 0 = both blank
        codes = sorted(rows)
        options = []
        for c in codes:
            v = rows[c]
            options.append([tuple(v[:2])] if len(v) >= 2 else [(v[0], 0), (0, v[0])] if v else [(0, 0)])
        fits = []
        for combo in itertools.product(*options):
            cur, prev = sum(a for a, _ in combo), sum(b for _, b in combo)
            if abs(cur - total[0]) <= 2e9 and abs(prev - total[1]) <= 2e9:
                fits.append(dict(zip(codes, combo)))
        if not fits:
            res.warn(f"p{page}: COGS rows F30-F36 do not add up to F3A {total} - OCR error? {rows}")
            return page, None
        if len({f.get("F35", (0, 0)) for f in fits}) > 1:
            res.warn(f"p{page}: F35 column ambiguous {[f.get('F35') for f in fits]}")
            return page, None
        return page, fits[0].get("F35", (0, 0))
    res.warn("P&L detail note (F3A) not found on the last 5 pages")
    return None, None


def _csv_volume(q, y):
    from CongTyDauKhi import read_csv

    _, rows = read_csv(TICKER)
    return dict(rows).get(f"{q}Q{y}", {}).get(VOL)


# --------------------------------------------------------------------------

def collect(q, y):
    res = Result()
    path, source = _report(q, y, res)
    if not path:
        res.warn(f"BCTC hop nhat Q{q}/{y} chua co tren CafeF/Vietstock/petrolimex.com.vn (thuong ~30 ngay sau quy)")
        return res
    res.sources.append(source)

    page, pnl = _pnl(path, res)
    if {"10", "20"} <= set(pnl):
        nr, gp = to_bn(pnl["10"]), to_bn(pnl["20"])
        res.values[NR], res.values[GP] = nr, gp
        if "11" in pnl and abs(nr - to_bn(pnl["11"]) - gp) > max(1.0, abs(gp) * 0.01):
            res.warn(f"P&L p{page}: 10 - 11 = {nr - to_bn(pnl['11']):,.1f} != 20 = {gp:,.1f} - OCR error?")
        vol = _csv_volume(q, y)
        if vol:
            res.values[UNIT_GP] = gp / vol * 1000
            res.warn(f"{UNIT_GP} uses the Domestic Volume already in the CSV ({vol:g} k m3), not a disclosure")
    elif pnl:
        res.warn(f"P&L p{page}: codes read {sorted(pnl)}, need 10 and 20")

    page, ytd = _cogs_note(path, res)
    if ytd is not None:
        if q == 1:
            res.values[PROV] = to_bn(ytd[0])
        else:
            pq, py = prev_quarter(q, y)
            prev_path, prev_source = _report(pq, py, res)
            prev_ytd = None
            if prev_path:
                _, prev = _cogs_note(prev_path, res)
                prev_ytd = prev and prev[0]
            if prev_ytd is None:
                res.warn(f"{PROV}: F35 is year-to-date ({to_bn(ytd[0]):,.1f}); {pq}Q{py} report unreadable -> left out")
            else:
                res.sources.append(prev_source)
                res.values[PROV] = to_bn(ytd[0] - prev_ytd)
                res.warn(f"{PROV} = YTD F35 {to_bn(ytd[0]):,.1f} - {pq}Q{py} YTD {to_bn(prev_ytd):,.1f}")
    return res
