"""Shared plumbing for the per-ticker disclosure parsers (disclosures/<ticker>.py).

Finding documents: utility/fetch.py already scrapes every covered company's IR
pages (and HNX / CafeF) into dc_commodity.TinNganhDien, one document per item
with {title, url, date, tickers[], type}. `find_documents` searches that index,
so a parser only needs a title pattern - no per-site scraper. `company_page`
covers sites the index misses (pass the IR list URL; links are read from HTML).

Reading documents: `pdf_text` uses the text layer (pdfplumber); scanned PDFs
fall back to OCR (`ocr_pages`: PyMuPDF renders, RapidOCR reads - pure pip, no
Tesseract/Poppler install needed).
"""

import hashlib
import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from ThiTruongKhoan import DATABASE_NAME, connect_mongo  # noqa: E402

CACHE = os.path.join(HERE, "downloads")  # gitignored
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


@dataclass
class Result:
    """What a parser hands back to update_disclosure.py."""
    values: dict = field(default_factory=dict)      # CSV column -> number
    sources: list = field(default_factory=list)     # urls / files read
    warnings: list = field(default_factory=list)    # REVIEW notes for the analyst

    def warn(self, text):
        self.warnings.append(text)


# --------------------------------------------------------------------------
# quarters
# --------------------------------------------------------------------------

def label(q, y):
    return f"{q}Q{y}"


def prev_quarter(q, y):
    return (4, y - 1) if q == 1 else (q - 1, y)


def period_words(q, y):
    """Ways a Vietnamese title names the period of quarter q (incl. half-year / 9M reports)."""
    roman = {1: "I", 2: "II", 3: "III", 4: "IV"}[q]
    words = [rf"qu[ýy]\s*0?{q}\b", rf"qu[ýy]\s*{roman}\b", rf"q\s*0?{q}\b", rf"\b{q}q"]
    return r"(?:" + "|".join(words) + r")[^0-9]{0,15}" + str(y)


# --------------------------------------------------------------------------
# finding documents
# --------------------------------------------------------------------------

def find_documents(ticker, title_regex, since=None, until=None, types=("document", "press", "news"), limit=50):
    """Newest-first TinNganhDien items of `ticker` whose title matches `title_regex` (case-insensitive)."""
    query = {"tickers": ticker, "type": {"$in": list(types)},
             "title": {"$regex": title_regex, "$options": "i"}}
    span = {}
    if since:
        span["$gte"] = since if isinstance(since, datetime) else datetime.fromisoformat(since)
    if until:
        span["$lte"] = until if isinstance(until, datetime) else datetime.fromisoformat(until)
    if span:
        query["date"] = span
    client = connect_mongo()
    try:
        cur = client[DATABASE_NAME]["TinNganhDien"].find(
            query, {"_id": 0, "title": 1, "url": 1, "date": 1, "type": 1, "dataset": 1}).sort("date", -1).limit(limit)
        return list(cur)
    finally:
        client.close()


def _flat(text):
    """Accent-free lowercase, for matching Vietnamese titles ("Hợp nhất" -> "hop nhat")."""
    import unicodedata

    text = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in text if unicodedata.category(c) != "Mn").lower()


def _bctc_kind(title):
    """'quarter' / 'reviewed' (6M soát xét) / 'audited' (năm kiểm toán), or None when the title is
    not a consolidated statement (parent-only, explanations, auditor contracts...)."""
    t = _flat(title)
    if "hop nhat" not in t or re.search(r"cong ty me|rieng|giai trinh|hop dong|ky ket|nghi quyet", t):
        return None
    if "kiem toan" in t:
        return "audited"
    if "soat xet" in t or "ban nien" in t or "6 thang" in t:
        return "reviewed"
    return "quarter"


def find_bctc(ticker, q, y, website=None):
    """Consolidated financial statements of quarter q/y - CafeF's list, Vietstock's and, when the
    parser passes `website` (a callable (q, y) -> [{title, kind, url}], e.g. a `site_bctc` partial),
    the company's own IR page, all asked in parallel. Returns [{title, kind, url, site}], the
    quarterly report (which has a this-quarter column) before the reviewed / audited ones."""
    from concurrent.futures import ThreadPoolExecutor

    listers = [("cafef", lambda: _cafef_bctc(ticker, q, y)), ("vietstock", lambda: _vietstock_bctc(ticker, q, y))]
    if website:
        listers.append(("website", lambda: website(q, y)))
    found = []
    with ThreadPoolExecutor(len(listers)) as pool:
        jobs = {site: pool.submit(lister) for site, lister in listers}
        for site, job in jobs.items():
            try:
                found += [dict(d, site=site) for d in job.result()]
            except Exception as exc:  # one site down must not hide the other
                print(f"  ! {site}: {type(exc).__name__}: {exc}", file=sys.stderr)
    order = {"quarter": 0, "reviewed": 1, "audited": 2}
    return sorted(found, key=lambda d: order[d["kind"]])


def fetch_bctc(ticker, q, y, kinds=("quarter",), website=None):
    """Download the quarter's consolidated BCTC from whichever site answers first.

    Every copy of the best available kind (CafeF, Vietstock and the company website usually all
    have it) is fetched in parallel; the first complete PDF wins. Returns
    (path, {title, kind, url, site}) or (None, None) when no site lists the report yet."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    docs = find_bctc(ticker, q, y, website)
    for kind in kinds:
        cands = [d for d in docs if d["kind"] == kind]
        if not cands:
            continue
        for d in cands:  # a copy downloaded (and OCR'd) before wins outright - keeps the OCR cache warm
            if os.path.exists(cached_path(d["url"], ticker)):
                return cached_path(d["url"], ticker), d
        pool = ThreadPoolExecutor(len(cands))
        pending = {pool.submit(download, d["url"], ticker): d for d in cands}
        try:
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for job in done:
                    d = pending.pop(job)
                    try:
                        return job.result(), d
                    except Exception as exc:
                        print(f"  ! {d['site']}: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    return None, None


def _cafef_bctc(ticker, q, y):
    import json

    url = f"https://cafef.vn/du-lieu/Ajax/PageNew/FileBCTC.ashx?Symbol={ticker}&Type=1&Year={y}"
    rows = json.loads(fetch(url)).get("Data") or []
    out = []
    for r in rows:
        kind = _bctc_kind(r.get("Name") or "")
        if kind and r.get("Quarter") == q and r.get("Year") == y and r.get("Link"):
            out.append({"title": r["Name"], "kind": kind, "url": r["Link"]})
    return out


def title_matches(title, kind, q, y):
    """Does a BCTC title name quarter q/y? Reviewed half-year reports count for Q2, audited
    annual ones for Q4 (they carry the full year, used only when no quarterly report exists)."""
    t = _flat(title)
    if kind == "quarter":
        return bool(re.search(period_words(q, y), t))
    if kind == "reviewed":
        return q == 2 and str(y) in t
    return q == 4 and str(y) in t and not re.search(r"qu[yý]\s*[1-3]|q\s*[1-3]", t)


def site_bctc(url, q, y):
    """BCTC links of a company IR page that lists reports as plain <a href=...pdf>title</a>
    (ptsc.com.vn, pvdrilling.com.vn...): [{title, kind, url}] for quarter q/y."""
    html = fetch(url).decode("utf-8", "replace")
    out = []
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.S | re.I):
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()
        kind = _bctc_kind(title)
        if kind and title_matches(title, kind, q, y):
            link = urljoin(url, href.replace("&amp;", "&"))
            if link not in [d["url"] for d in out]:
                out.append({"title": title, "kind": kind, "url": link})
    return out


VIETSTOCK_PAGE = "https://finance.vietstock.vn/DPM/tai-tai-lieu.htm"
VIETSTOCK_API = "https://finance.vietstock.vn/data/getdocument"


def _vietstock_bctc(ticker, q, y):
    """Vietstock document list (CSRF flow, as in the filings-analyzer skill); type 1 = BCTC.
    Vietstock titles carry the period in words, so the quarter is matched on the title."""
    import requests

    with requests.Session() as http:
        http.headers["User-Agent"] = UA
        page = http.get(VIETSTOCK_PAGE, timeout=30).text
        m = re.search(r'name="?__RequestVerificationToken"?[^>]*value="?([^"\s>]+)', page)
        if not m:
            raise RuntimeError("Vietstock CSRF token not found")
        rows = http.post(VIETSTOCK_API, data={"code": ticker, "type": "1", "page": "1", "pageSize": "20",
                                              "__RequestVerificationToken": m.group(1)}, timeout=30).json()
    period = re.compile(period_words(q, y), re.I)
    six = q == 2 and re.compile(rf"(6 th[aá]ng|b[aá]n ni[eê]n).*{y}", re.I)
    out = []
    for r in rows if isinstance(rows, list) else []:
        title, link = (r.get("FullName") or r.get("Title") or "").strip(), (r.get("Url") or "").strip()
        kind = _bctc_kind(title)
        if kind and link and (period.search(title) or (six and six.search(title))):
            out.append({"title": title, "kind": kind, "url": link})
    return out


def company_page(url, link_regex):
    """[(text, absolute url)] of the links on an IR list page whose text or href matches `link_regex`."""
    html = fetch(url).decode("utf-8", "replace")
    out = []
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, flags=re.S | re.I):
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if re.search(link_regex, text + " " + href, flags=re.I):
            out.append((text, urljoin(url, href)))
    return out


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------

def fetch(url, timeout=90):
    import requests

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # some IR sites have broken TLS chains -> verify=False retry
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        except requests.exceptions.SSLError:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, verify=False)
    r.raise_for_status()
    return r.content


def cached_path(url, ticker, suffix=".pdf"):
    return os.path.join(CACHE, ticker, hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + suffix)


def download(url, ticker, suffix=".pdf"):
    """Cached download -> local path. HTML pages that wrap a PDF (pvgas, ptsc...) are followed
    to the first PDF link on the page."""
    os.makedirs(os.path.join(CACHE, ticker), exist_ok=True)
    path = cached_path(url, ticker, suffix)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    data = fetch(url)
    if suffix == ".pdf" and data.startswith(b"PK"):  # Vietstock ships some filings as a ZIP
        data = _pdf_from_zip(data, url)
    if suffix == ".pdf" and not data.startswith(b"%PDF"):
        html = data.decode("utf-8", "replace")
        links = re.findall(r'(?:href|src|data)="([^"]+\.pdf[^"]*)"', html, flags=re.I)
        if not links:
            raise RuntimeError(f"{url}: neither a PDF nor a page linking one")
        data = fetch(urljoin(url, links[0].replace("&amp;", "&")))
        if not data.startswith(b"%PDF"):
            raise RuntimeError(f"{url}: linked file is not a PDF")
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _pdf_from_zip(data, url):
    """The consolidated statement inside a filing ZIP (parent-only, auditor letters... skipped)."""
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        pdfs = [m for m in zf.infolist() if m.filename.lower().endswith(".pdf")]
        if not pdfs:
            raise RuntimeError(f"{url}: ZIP without a PDF")
        flat = lambda m: _flat(m.filename).replace("_", " ")  # noqa: E731
        best = ([m for m in pdfs if "hop nhat" in flat(m) or "hopnhat" in flat(m)]
                or [m for m in pdfs if not re.search(r"cong ty me|congtyme|rieng", flat(m))] or pdfs)
        return zf.read(max(best, key=lambda m: m.file_size).filename)


def pdf_pages(path):
    return len(text_layer(path))


def pdf_text(path, pages=None, ocr_fallback=True):
    """{page number (1-based): text}. Pages without a text layer are OCR'd when `ocr_fallback`."""
    import pdfplumber

    out = {}
    with pdfplumber.open(path) as pdf:
        numbers = pages or range(1, len(pdf.pages) + 1)
        for n in numbers:
            out[n] = pdf.pages[n - 1].extract_text() or ""
    if ocr_fallback:
        empty = [n for n, t in out.items() if len(t.strip()) < SCANNED_BELOW]
        if empty:
            out.update(ocr_pages(path, empty))
    return out


# Where the footnotes (Thuyết minh) start - VAS Circular 200 puts them after B01/B02/B03.
# Patterns are matched on accent-free uppercase OCR text (RapidOCR drops diacritics and spaces).
FOOTNOTE_START = [r"THUYET\s*MINH\s*BAO\s*CAO\s*TAI\s*CHINH", r"DAC\s*DIEM\s*HOAT\s*DONG", r"B\s*0?9a?\s*-\s*DN"]


def footnote_start(path, scan_limit=25):
    """First page of the Thuyết minh, found as in the filings-analyzer skill: a cheap low-resolution
    OCR of each page until the section title shows up. Text-layer pages are read as text."""
    for n in range(1, min(scan_limit, pdf_pages(path)) + 1):
        if any(re.search(p, _flat(page_text(path, n, cheap=True)).upper()) for p in FOOTNOTE_START):
            return n
    return None


HINTS = os.path.join(CACHE, "page_hints.json")


def locate_pages(path, pattern, start=None, end=None, hint=None, first_only=False):
    """Pages whose text matches `pattern` (accent-free regex, e.g. "gia von") - read only those
    afterwards, never the whole report. Scanned pages are screened with a cheap low-resolution OCR.

    `hint` names a remembered position (e.g. "GAS:segment"): the same note sits at about the same
    relative place in every quarter's report, so screening starts there and walks outwards, and
    with `first_only` stops at the first hit - a few OCR'd pages instead of the whole footnotes."""
    import json

    total = pdf_pages(path)
    first, last = start or footnote_start(path) or 1, end or total
    pages = list(range(first, last + 1))
    hints = {}
    if hint:
        try:
            with open(HINTS, encoding="utf-8") as handle:
                hints = json.load(handle)
        except (OSError, ValueError):
            pass
        if hint in hints:
            centre = round(hints[hint] * total)
            pages.sort(key=lambda n: abs(n - centre))
    hits = []
    for n in pages:
        if re.search(pattern, _flat(page_text(path, n, cheap=True)), re.I):
            hits.append(n)
            if first_only:
                break
    if hint and hits:
        hints[hint] = min(hits) / total
        os.makedirs(CACHE, exist_ok=True)
        with open(HINTS, "w", encoding="utf-8") as handle:
            json.dump(hints, handle, indent=1)
    return sorted(hits)


SCREEN_DPI = 100  # 60 DPI garbles scanned headings ("THIUVET MINI"); 100 reads them at the same speed
# Scanned pages often still carry a one-line text layer (an e-signature stamp: "Văn bản này được
# xác thực tại ..."), so a page counts as scanned below this many characters of text.
SCANNED_BELOW = 300
_LAYER = {}


def text_layer(path):
    """{page: text layer} of a PDF, read once per file."""
    if path not in _LAYER:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            _LAYER[path] = {i + 1: (pg.extract_text() or "") for i, pg in enumerate(pdf.pages)}
    return _LAYER[path]


def page_text(path, n, cheap=False):
    """Text of page n: the text layer when there is one, else OCR - full resolution, or a
    screening pass at SCREEN_DPI when `cheap`. OCR results are cached next to the PDF."""
    text = text_layer(path)[n]
    if len(text.strip()) >= SCANNED_BELOW:
        return text
    return ocr_pages(path, [n], zoom=SCREEN_DPI / 72 if cheap else 3.0)[n]


_OCR = None


def ocr_pages(path, pages, zoom=3.0):
    """{page: text} by OCR (PyMuPDF render + RapidOCR). Lines are rebuilt from box positions so
    table rows stay on one line. Cached per PDF and resolution in <pdf>.ocr.json."""
    global _OCR
    import json

    cache_file = path + ".ocr.json"
    try:
        with open(cache_file, encoding="utf-8") as handle:
            cache = json.load(handle)
    except (OSError, ValueError):
        cache = {}
    key = lambda n: f"{n}@{zoom:.2f}"  # noqa: E731
    todo = [n for n in pages if key(n) not in cache]
    if todo:
        import fitz  # PyMuPDF
        from rapidocr_onnxruntime import RapidOCR

        _OCR = _OCR or RapidOCR()
        doc = fitz.open(path)
        try:
            for n in todo:
                png = doc[n - 1].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
                boxes, _ = _OCR(png)
                cache[key(n)] = _rows(boxes or [], gap=12 * zoom / 3)
        finally:
            doc.close()
        with open(cache_file, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False)
    return {n: cache[key(n)] for n in pages}


def _rows(boxes, gap=12):
    """RapidOCR boxes -> text lines, joining boxes whose vertical centres are within `gap` px."""
    items = sorted(((sum(p[1] for p in b) / 4, b[0][0], t) for b, t, _ in boxes))
    lines, cur, cy = [], [], None
    for y, x, t in items:
        if cy is not None and abs(y - cy) > gap:
            lines.append(cur)
            cur = []
        cur.append((x, t))
        cy = y if not cur[:-1] else (cy + y) / 2
    if cur:
        lines.append(cur)
    return "\n".join("  ".join(t for _, t in sorted(line)) for line in lines)


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------

NUM = r"\(?-?\d{1,3}(?:[.,]\d{3})+(?:,\d+)?\)?|\(?-?\d+(?:,\d+)?\)?"


def vn_number(text):
    """'1.234.567' -> 1234567 ; '(1.234)' -> -1234 ; '1,34' -> 1.34 ; '12,345,678' (EN) -> 12345678."""
    if text is None:
        return None
    s = str(text).strip().replace(" ", "")
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = s.strip("()-")
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):          # VN thousands with dots
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", s):        # EN thousands with commas
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def numbers_in(line):
    return [vn_number(m) for m in re.findall(NUM, line)]


def to_bn(vnd):
    """VND -> VND bn."""
    return None if vnd is None else vnd / 1e9


def find_line(text, pattern, start=0):
    """First line (from character `start`) matching `pattern`, with the numbers on it."""
    for line in text[start:].splitlines():
        if re.search(pattern, line, flags=re.I):
            return line, [n for n in numbers_in(line) if n is not None]
    return None, []
