"""
PV Gas (GAS) - quarter figures from the consolidated BCTC and PV Gas's investor bulletin.

1) The quarterly consolidated BCTC from CafeF or Vietstock, whichever downloads first
   (common.fetch_bctc); the pvgas.com.vn article (found through TinNganhDien) is a fallback.
   pvgas.com.vn wraps files as DesktopModules/EasyDNNNews/DocumentDownload.ashx links (no
   ".pdf" in the url) -> `_attachments()` / `_get()` here.
   Not used: parent-only ("công ty mẹ"/"riêng"), reviewed 6M ("đã soát xét", mid Aug) and audited
   FY ("đã được kiểm toán", Feb-Mar) - the quarterly report has a this-quarter P&L column.
   Copies differ: Vietnamese only (CafeF 2Q2026, ~39 pages) or VN+EN in one PDF (1Q2026, ~76
   pages, EN half second) - labels below are matched in both languages.
   Scanned (text layer = signature stamp only) -> OCR (common.page_text), two pages:
   - P&L "CONSOLIDATED INCOME STATEMENT" (~page 5 of 35): first amount on a line = this quarter.
         line 1  Revenue from sales of goods    (gross, before deductions - check only)
         line 3  Net revenues                   -> Net revenue (VNDbn)
         line 4  Cost of goods sold             (check only)
         line 5  Gross profit                   -> Gross profit (VNDbn)
   - Notes 25 "REVENUE FROM SALES OF GOODS AND RENDERING OF SERVICES" and 27 "COST OF GOODS SOLD"
     (same page, ~0.8 into the report). Rows dry gas / LPG / CNG / condensate / transportation of
     gas and condensate / leasing office / others ->
         <segment> net revenue (VNDbn)  = note 25 row (revenue BEFORE sales deductions, as in the CSV)
         <segment> gross profit (VNDbn) = note 25 row - note 27 row
     "Dry gas" includes regasified LNG (-> "Dry gas & LNG").
     These notes are YEAR-TO-DATE ("Accumulated from the beginning to ending of period") ->
     quarter = this report - previous quarter's report (Q1 as is; Q4 = FY report - 9M report).
     Checked: 2Q2026 dry gas 47,191.08 - 20,950.25 = 26,240.83 (CSV 26,241.08).
     The thông tin bộ phận note itself only splits gas business vs auxiliary and Vietnam vs
     Singapore - not by product - so it is not used.
   Validation: rows vs the note total (exact VND, so > 1bn = OCR digit error), note totals vs P&L
   YTD lines 1 / 4, quarter segment sums vs P&L quarter (revenue vs line 1, GP vs line 1 - line 4),
   NR - COGS = GP.

2) "Bản tin nhà đầu tư Quý X/Y" straight from pvgas.com.vn (fixed article slug, `_site_bulletin`;
   TinNganhDien as fallback) - text-layer PDF, ~1 week after the FS; slide 2 "SẢN LƯỢNG KHÍ":
   - Tiêu thụ: Khí khô (gồm LNG)  -> Total Volume (mn m3)
               Khách hàng điện    -> Power Volume (mn m3)
               Khách hàng đạm     -> Fertilizer Volume (mn m3)
               Khách hàng TA/CNG  -> Industrial & CNG Volume (mn m3)
               Condensate         -> Condensate Volume (k tons)
   - Sản xuất: LPG                -> LPG production (k tons)
   Column order is plan (KH), actual (TH), % -> the 2nd number; the actual sometimes wraps to the
   next line. Figures are YEAR-TO-DATE from Q2 on ("LŨY KẾ") -> quarter = this bulletin - previous
   one; the months covered are read from the header ("5T2025", "6 THÁNG", "NĂM 2025", "QUÝ 2")
   and a bulletin that does not cover exactly 3*q months is not used (the "Quý I/2025" bulletin
   covers 5 months). Bulletin figures are rounded (condensate 35,2) -> quarter differences carry
   +-0.5 rounding.

Not sourced (left out):
- LPG Volume (k tons): the bulletin line is "LPG/LNG incl. import-export & international trading"
  (1Q2026 1,315 vs CSV 1,243; 6M2025 1,692 vs CSV 1H 1,819; FY2025 printed as 374 = production) -
  not the CSV's definition.
- LNG input (mn m3): bulletin gives "Khí vào bờ + LNG nhập khẩu" only as one number.
- Brent (USD/bbl): not in either document.

Caveats: OCR mixes '.' and ',' as thousand separators and sometimes drops a digit of the last
group ("68,967,158,848,77"); amounts are whole VND so every 3-digit group is thousands, and a row
whose amount ends in a short group is rebuilt from the note total (flagged). Restated prior
quarters are not detected - the previous YTD comes from the previous quarterly report.
OCR is 20 s - 3 min per page depending on load; text is cached in <pdf>.ocr.json (common.ocr_pages).
"""

import hashlib
import os
import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urljoin

from disclosures.common import (CACHE, Result, fetch, find_documents, numbers_in, page_text, pdf_pages,
                                pdf_text, period_words, to_bn)

from disclosures.common import fetch_bctc  # noqa: E402

TICKER = "GAS"
NR, GP = "Net revenue (VNDbn)", "Gross profit (VNDbn)"
SEGMENTS = ["Dry gas & LNG", "LPG", "CNG", "Transportation", "Condensate", "Others", "Office leasing"]
# note 25 / 27 row -> segment; transportation ("of gas and condensate") and office before the products
ROWS = [("Transportation", r"transport|van\s*chuyen"),
        ("Office leasing", r"office|van\s*phong"),
        ("Dry gas & LNG", r"dry\s*gas|khi\s*kho"),
        ("LPG", r"lpg"),  # OCR glues words ("sales ofLPG")
        ("CNG", r"\bcng"),
        ("Condensate", r"condensate"),
        ("Others", r"^\W*others?\b|^\W*khac\b|(doanh\s*thu|gia\s*von)\s*khac\b")]
VOLUMES = [("Total Volume (mn m3)", "use", r"^-?\s*khi\s*kho"),
           ("Power Volume (mn m3)", "use", r"khach\s*hang\s*dien"),
           ("Fertilizer Volume (mn m3)", "use", r"khach\s*hang\s*dam"),
           ("Industrial & CNG Volume (mn m3)", "use", r"khach\s*hang\s*ta|khach\s*hang.*cng"),
           ("Condensate Volume (k tons)", "use", r"^-?\s*condensate"),
           ("LPG production (k tons)", "make", r"^-?\s*lpg\b(?!\s*/)")]
AMOUNT = r"\(?-?\d{1,3}(?:[.,]\s?\d{3})+(?:[.,]\d{1,2}(?!\d))?\)?"
NOT_QUARTERLY = r"cong\s*ty\s*me|rieng|soat\s*xet|kiem\s*toan|giua\s*nien|ban\s*nien|6\s*thang"


def _flat(text):
    """Accent-free lowercase ("Hợp nhất" -> "hop nhat"); common._flat is private."""
    text = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


def _amount(text):
    """'1.086.055,224.203' -> 1086055224203. A trailing 1-2 digit group (OCR dropped a digit)
    -> (value, True) so the caller can rebuild it from the total."""
    neg = text.startswith("(") or text.startswith("-")
    groups = re.findall(r"\d+", text)
    short = len(groups) > 1 and len(groups[-1]) < 3
    v = int("".join(groups[:-1] if short else groups))
    if short:
        v = v * 1000  # magnitude only; flagged
    return (-v if neg else v), short


def _amounts(line):
    return [_amount(m) for m in re.findall(AMOUNT, line)]


# --------------------------------------------------------------------------
# finding and downloading
# --------------------------------------------------------------------------

def _docs(q, y, kind):
    """TinNganhDien items of the quarter, newest first. kind 'fs' / 'bulletin'."""
    end = datetime(y, q * 3, 28)
    docs = find_documents(TICKER, r"t[àa]i\s*ch[íi]nh|nh[àa]\s*[đd][ầa]u\s*t[ưu]|FS|investor",
                          since=end, until=end + timedelta(days=200), types=("document", "press", "news"))
    period = re.compile(period_words(q, y), re.I)
    out = []
    for d in docs:
        t = _flat(d["title"])
        if kind == "bulletin":
            ok = "ban tin nha dau tu" in t and period.search(t)
        else:
            ok = "tai chinh" in t and not re.search(NOT_QUARTERLY, t) and (
                period.search(t) or (q == 4 and re.search(rf"hop\s*nhat\s*nam\s*{y}", t)))
        if ok:
            out.append(d)
    if kind == "fs":  # consolidated first; "Báo cáo tài chính Q1.2025" (no hợp nhất) carries both
        out.sort(key=lambda d: "hop nhat" not in _flat(d["title"]))
    return out


def _attachments(url):
    """[(file name, absolute url)] of the DocumentDownload links of a pvgas article page."""
    html = fetch(url).decode("utf-8", "replace")
    out = []
    for href, text in re.findall(r'<a[^>]+href="([^"]*DocumentDownload\.ashx[^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I):
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        link = urljoin(url, href.replace("&amp;", "&"))
        if link not in [u for _, u in out]:
            out.append((name, link))
    return out


def _get(url):
    """Cached download of an attachment (or a direct PDF url) -> local path."""
    os.makedirs(os.path.join(CACHE, TICKER), exist_ok=True)
    path = os.path.join(CACHE, TICKER, hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".pdf")
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        data = fetch(url)
        if not data.startswith(b"%PDF"):
            raise RuntimeError(f"{url}: not a PDF")
        with open(path, "wb") as handle:
            handle.write(data)
    return path


def _pdfs(doc, prefer):
    """PDF paths of a TinNganhDien item, the attachment whose name matches `prefer` first."""
    url = doc["url"]
    if re.search(r"\.pdf($|\?)", url, re.I):
        return [(url, _get(url))]
    files = _attachments(url)
    files = [f for f in files if not re.search(r"cong ty me|rieng|parent|separate", _flat(f[0]))]
    files.sort(key=lambda f: not re.search(prefer, _flat(f[0])))
    return [(u, _get(u)) for _, u in files]


# --------------------------------------------------------------------------
# financial statements
# --------------------------------------------------------------------------

def _ocr(path, n):
    return page_text(path, n)  # text layer, else full-resolution OCR (shared cache)


def _search(path, frac, test, width=8):
    """First page passing `test`, trying pages nearest `frac` of each language half (EN half first)."""
    n = pdf_pages(path)
    halves = [(1, n)] if n <= 50 else [(n // 2 + 1, n), (1, n // 2)]
    for s, e in halves:
        centre = s + frac * (e - s)
        for p in sorted(range(s, e + 1), key=lambda p: abs(p - centre))[:width]:
            text = _ocr(path, p)
            if test(text):
                return p, text
    return None, None


def _is_pnl(text):
    t = _flat(text)
    # OCR garbles single words ("Lgi nhuan gop", "INCOMESTATEMENT"), so any one title + any one P&L line will do
    return bool(re.search(r"income\s*statement|statement\s*of\s*income|ket\s*qua\s*hoat\s*dong", t)
                and re.search(r"gross\s*profit|nhuan\s*gop|net\s*revenue|doanh\s*thu\s*thuan", t))


def _is_note(text):
    t = _flat(text)
    return bool(re.search(r"revenue\s*from\s*sales\s*of\s*goods|doanh\s*thu\s*ban\s*hang", t)
                and re.search(r"dry\s*gas|khi\s*kho", t))


def _pnl(path, res):
    """{line: (this quarter, YTD) VND} for lines 1 / 2 / 3 / 4 / 5 of the P&L."""
    page, text = _search(path, 0.12, _is_pnl)
    if not page:
        res.warn(f"{os.path.basename(path)}: P&L page not found")
        return {}
    pats = {1: r"^\W*1\s*[.,]?\s*revenue|^\W*1\s*[.,]?\s*doanh\s*thu", 2: r"sales\s*deduction|giam\s*tru",
            3: r"net\s*revenue|doanh\s*thu\s*thuan", 4: r"cost\s*of\s*goods|gia\s*von",
            5: r"gross\s*profit|loi\s*nhuan\s*gop"}
    out = {}
    for line in text.splitlines():
        t = _flat(line)
        for k, pat in pats.items():
            if k not in out and re.search(pat, t):
                nums = [v for v, _ in _amounts(line)]  # short last group: approximate, keeps column order
                if nums:
                    out[k] = (nums[0], nums[2] if len(nums) >= 3 else nums[0])
                break
    return out


def _block(lines, res, where):
    """{segment: VND} + total from the rows of one note (first amount = current year)."""
    rows, total, rebuild, pending = {}, None, None, ""
    is_row = lambda lab: any(re.search(p, lab) for _, p in ROWS)  # noqa: E731
    for i, line in enumerate(lines):
        amts = _amounts(line)
        t = _flat(line)
        label = re.sub(AMOUNT, " ", t)
        if not amts:
            if pending == "<taken>":  # label of amounts already booked from the line above
                pending = ""
                continue
            pending = label  # a label whose amounts wrap to the next line (VN layout)
            continue
        if not re.search(r"[a-z]{3}", label):  # numbers only -> the note total, or a wrapped row
            nxt = _flat(lines[i + 1]) if i + 1 < len(lines) else ""
            if pending and is_row(pending) and len(rows) < len(SEGMENTS):
                label = pending
            elif nxt and not _amounts(lines[i + 1]) and is_row(nxt) and len(rows) < len(SEGMENTS):
                label, pending = nxt, "<taken>"  # OCR put the amounts just above their label
            else:
                total = amts[0][0]
                break
        if re.match(r"^\W*(tong\s*)?cong\b|^\W*total\b", label):  # "Cộng 68.967..." on one line
            total = amts[0][0]
            break
        pending = "<taken>" if pending == "<taken>" else ""
        for seg, pat in ROWS:
            if re.search(pat, label) and seg not in rows:
                rows[seg] = amts[0][0]
                if amts[0][1]:
                    rebuild = seg
                break
    if total is None:
        res.warn(f"{where}: note total not read")
        return rows, None
    if rebuild and len(rows) == len(SEGMENTS):
        rows[rebuild] = total - sum(v for s, v in rows.items() if s != rebuild)
        res.warn(f"{where}: {rebuild} amount OCR'd with a short last group - rebuilt from the note total")
    missing = [s for s in SEGMENTS if s not in rows]
    if missing:
        res.warn(f"{where}: rows not read {missing}")
    elif abs(sum(rows.values()) - total) > 1e9:
        res.warn(f"{where}: rows add up to {to_bn(sum(rows.values())):,.2f} vs note total {to_bn(total):,.2f} - OCR digit error?")
    return rows, total


def _notes(path, res):
    """YTD {segment: (revenue, COGS) VND} + note totals from notes 25 and 27."""
    page, text = _search(path, 0.78, _is_note)
    if not page:
        res.warn(f"{os.path.basename(path)}: revenue note (25) not found")
        return None
    lines = text.splitlines()
    if not re.search(r"cost\s*of\s*goods\s*sold|gia\s*von\s*hang\s*ban", _flat(text)):
        lines += _ocr(path, page + 1).splitlines()  # note 27 spilled to the next page
    flat = [_flat(l) for l in lines]
    i_rev = next(i for i, t in enumerate(flat) if re.search(r"revenue\s*from\s*sales\s*of\s*goods|doanh\s*thu\s*ban\s*hang", t))
    i_cogs = next((i for i, t in enumerate(flat) if i > i_rev and re.search(r"cost\s*of\s*goods\s*sold|gia\s*von\s*hang\s*ban", t)), None)
    if i_cogs is None:
        res.warn(f"{os.path.basename(path)} p{page}: COGS note (27) not found")
        return None
    where = f"{os.path.basename(path)} p{page}"
    rev, rev_total = _block(lines[i_rev + 1:i_cogs], res, where + " note 25")
    cogs, cogs_total = _block(lines[i_cogs + 1:], res, where + " note 27")
    return {"rev": rev, "cogs": cogs, "rev_total": rev_total, "cogs_total": cogs_total}


def _fs(q, y, res):
    """(P&L lines, YTD notes, source url) of the quarterly consolidated report, or (None, None, None).
    The report comes from CafeF / Vietstock (common.fetch_bctc, whichever downloads first); the
    pvgas.com.vn article (TinNganhDien) is only a fallback when neither lists it."""
    path, doc = fetch_bctc(TICKER, q, y)
    files = [(f"{doc['title']} ({doc['site']}) {doc['url']}", path)] if path else []
    if not files:
        docs = _docs(q, y, "fs")
        if not docs:
            return None, None, None
        try:
            files = _pdfs(docs[0], r"consolidated|hop nhat|english|\ben\b")
        except Exception as exc:
            res.warn(f"{docs[0]['url']}: download failed: {exc}")
            return None, None, None
    for url, path in files:
        pnl = _pnl(path, res)
        notes = _notes(path, res)
        if pnl and notes:
            return pnl, notes, url
    return None, None, None


def _financials(q, y, res):
    pnl, notes, url = _fs(q, y, res)
    if not pnl:
        res.warn(f"BCTC hop nhat Q{q}/{y} chua co tren CafeF/Vietstock/pvgas.com.vn - chua cong bo?")
        return
    res.sources.append(url)
    if 3 in pnl:
        res.values[NR] = to_bn(pnl[3][0])
    if 5 in pnl:
        res.values[GP] = to_bn(pnl[5][0])
    if all(k in pnl for k in (3, 4, 5)) and abs(pnl[3][0] - pnl[4][0] - pnl[5][0]) > 1e9:
        res.warn(f"P&L: net revenue - COGS != gross profit ({to_bn(pnl[3][0] - pnl[4][0]):,.2f} vs {to_bn(pnl[5][0]):,.2f})")
    for k, tot in ((1, "rev_total"), (4, "cogs_total")):
        if k in pnl and notes[tot] and abs(pnl[k][1] - notes[tot]) > 1e9:
            res.warn(f"note {'25' if k == 1 else '27'} total {to_bn(notes[tot]):,.2f} != P&L YTD line {k} {to_bn(pnl[k][1]):,.2f}")

    rev, cogs = dict(notes["rev"]), dict(notes["cogs"])
    if q > 1:  # notes are YTD -> subtract the previous quarterly report
        _, prev, prev_url = _fs(q - 1, y, res)
        if not prev:
            res.warn(f"segments: notes are YTD and the Q{q - 1}/{y} report was not found - segments left out")
            return
        res.sources.append(prev_url)
        res.warn(f"segments = YTD Q{q} notes - YTD Q{q - 1} notes (previous quarterly report)")
        rev = {s: v - prev["rev"][s] for s, v in rev.items() if s in prev["rev"]}
        cogs = {s: v - prev["cogs"][s] for s, v in cogs.items() if s in prev["cogs"]}
    for s in SEGMENTS:
        if s in rev:
            res.values[f"{s} net revenue (VNDbn)"] = to_bn(rev[s])
        if s in rev and s in cogs:
            res.values[f"{s} gross profit (VNDbn)"] = to_bn(rev[s] - cogs[s])
    # quarter segment sums vs the P&L quarter column
    if len(rev) == len(SEGMENTS) and 1 in pnl:
        diff = sum(rev.values()) / pnl[1][0] - 1
        if abs(diff) > 0.01:
            res.warn(f"segment revenue sum {to_bn(sum(rev.values())):,.2f} vs P&L gross revenue {to_bn(pnl[1][0]):,.2f} ({diff:+.1%})")
    if len(cogs) == len(SEGMENTS) and len(rev) == len(SEGMENTS) and 1 in pnl and 4 in pnl:
        seg_gp, pl_gp = sum(rev.values()) - sum(cogs.values()), pnl[1][0] - pnl[4][0]
        if abs(seg_gp / pl_gp - 1) > 0.01:
            res.warn(f"segment GP sum {to_bn(seg_gp):,.2f} vs P&L gross revenue - COGS {to_bn(pl_gp):,.2f}")


# --------------------------------------------------------------------------
# investor bulletin
# --------------------------------------------------------------------------

def _months(header, q):
    """Months covered by the volume table, from its header text."""
    h = _flat(header)
    m = re.search(r"\b(\d{1,2})\s*t\s*\.?\s*20\d\d", h) or re.search(r"(\d{1,2})\s*thang", h)
    if m:
        return int(m.group(1))
    if re.search(r"luy\s*ke\s*nam|\bth\s*20\d\d\b", h):
        return 12
    m = re.search(r"\bq\s*(\d)\s*[.]\s*20\d\d|quy\s*(\d)\b|quy\s*(iv|iii|ii|i)\b|\bq(iv|iii|ii|i)\s*[.]", h)
    if m:
        g = next(x for x in m.groups() if x)
        qq = int(g) if g.isdigit() else {"i": 1, "ii": 2, "iii": 3, "iv": 4}[g]
        # "QUÝ 2" alone could be the quarter only - trust it as YTD only with "LŨY KẾ"
        return 3 * qq if qq == 1 or "luy ke" in h else 3
    return None


def _values(line, nxt):
    """Plan / actual numbers of a bulletin row (units and % dropped, wrapped actual joined)."""
    def clean(s):
        s = re.sub(r"tri[ệe]u\s*m3|ngh[ìi]n\s*t[ấa]n|t[ỷy]\s*đ[ồo]ng|\bm3\b", " ", s, flags=re.I)
        s = re.sub(r"\S*%", " ", s)
        s = re.sub(r"\(.*?\)", " ", s)
        return [v for v in numbers_in(s) if v is not None]
    nums = clean(re.sub(r"^\s*\d\s+", " ", line))
    if len(nums) < 2 and nxt is not None and not re.search(r"[A-Za-zÀ-ỹ]{2}", re.sub(r"\S*%", "", nxt)):
        nums += clean(nxt)
    return nums


IR_ARTICLE = "https://www.pvgas.com.vn/quan-he-co-%C4%91ong/ban-tin-nha-dau-tu-{}"


def _site_bulletin(q, y):
    """The quarter's investor bulletin straight from pvgas.com.vn: the article slug is fixed
    ("Bản tin nhà đầu tư Quý 2/2026" -> .../quan-he-co-đong/ban-tin-nha-dau-tu-quy-22026)."""
    for slug in (f"quy-{q}{y}", f"quy-{q}-{y}", f"quy-{q}-nam-{y}"):
        url = IR_ARTICLE.format(slug)
        try:
            html = fetch(url).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - 404 = not this slug
            continue
        # an unknown slug still answers 200 with the generic "Quan hệ cổ đông" page
        title = re.search(r"<title>(.*?)</title>", html, re.S)
        if title and re.search(rf"ban\s*tin\s*nha\s*dau\s*tu.*{period_words(q, y)}", _flat(title.group(1))):
            return [{"title": title.group(1).strip(), "url": url}]
    return []


def _bulletin_page(q, y, res):
    docs = _site_bulletin(q, y) or _docs(q, y, "bulletin")  # company website first, TinNganhDien as fallback
    if not docs:
        return None, None
    try:
        files = _pdfs(docs[0], r"ban tin|investor")
    except Exception as exc:
        res.warn(f"{docs[0]['url']}: download failed: {exc}")
        return None, None
    for url, path in files:
        for n, text in pdf_text(path, ocr_fallback=False).items():
            if re.search(r"ti[êe]u\s*th[ụu]", text, re.I) and re.search(r"kh[íi]\s*kh[ôo]", text, re.I):
                return url, text.split("KẾT QUẢ")[0]
    return None, None


def _bulletin(q, y, res):
    """({column: YTD value}, months covered, url) of the quarter's investor bulletin."""
    url, text = _bulletin_page(q, y, res)
    if not text:
        return None, None, None
    lines = [l.strip() for l in text.splitlines()]
    header = " ".join(lines[:4])
    months = _months(header, q)
    section, out = None, {}
    for i, line in enumerate(lines):
        t = _flat(line)
        if re.search(r"^\d?\s*san\s*xuat", t):
            section = "make"
        elif re.search(r"^\d?\s*tieu\s*thu", t):
            section = "use"
        for col, sec, pat in VOLUMES:
            if sec == section and col not in out and re.search(pat, t):
                nums = _values(line, lines[i + 1] if i + 1 < len(lines) else None)
                if len(nums) >= 2:
                    out[col] = nums[1]
                else:
                    res.warn(f"bulletin Q{q}/{y}: '{line}' - plan/actual not both read {nums}")
    return out, months, url


def _volumes(q, y, res):
    cur, months, url = _bulletin(q, y, res)
    if cur is None:
        res.warn(f"investor bulletin Q{q}/{y} not found (TinNganhDien) - not out yet?")
        return
    res.sources.append(url)
    if months != 3 * q:
        res.warn(f"bulletin Q{q}/{y} covers {months} months, not {3 * q} - volumes left out")
        return
    vals = dict(cur)
    if q > 1:  # cumulative ("LŨY KẾ") from Q2 on
        prev, pm, purl = _bulletin(q - 1, y, res)
        if not prev or pm != 3 * (q - 1):
            res.warn(f"volumes: bulletin is YTD and Q{q - 1}/{y} bulletin (YTD {3 * (q - 1)} months) "
                     f"not usable ({pm} months) - volumes left out")
            return
        res.sources.append(purl)
        res.warn(f"volumes = bulletin {3 * q}M - bulletin {3 * (q - 1)}M (rounded figures, +-0.5)")
        vals = {c: v - prev[c] for c, v in cur.items() if c in prev}
    parts = ["Power Volume (mn m3)", "Fertilizer Volume (mn m3)", "Industrial & CNG Volume (mn m3)"]
    total = vals.get("Total Volume (mn m3)")
    if total and all(p in vals for p in parts):
        s = sum(vals[p] for p in parts)
        if abs(s / total - 1) > 0.01:
            res.warn(f"dry gas customers add up to {s:,.0f} vs total {total:,.0f}")
    for col, v in vals.items():
        if v < 0:
            res.warn(f"{col}: negative quarter value {v} - skipped")
            continue
        res.values[col] = v


# --------------------------------------------------------------------------

def collect(q, y):
    res = Result()
    _financials(q, y, res)
    _volumes(q, y, res)
    return res
