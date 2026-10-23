"""
PV Drilling (PVD) - quarter figures from the quarterly consolidated financial statements.

Document: the quarterly consolidated BCTC ("Báo cáo tài chính hợp nhất quý X năm Y"), from
CafeF, Vietstock or pvdrilling.com.vn/quan-he-co-dong/bao-cao-tai-chinh (WEBSITE), whichever
downloads first (common.fetch_bctc). Published ~29-30 days after
quarter end (Q4 on ~30 Jan). The reviewed half-year ("Bán niên soát xét", late Aug) and the
audited FY ("năm ... đã kiểm toán", late Mar) are NOT used: the quarterly report already has a
this-quarter column, so no YTD subtraction is needed.

The PDF is a scan (no text layer) -> OCR (common.ocr_pages, RapidOCR), only two pages:

- P&L "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH HỢP NHẤT" (page ~5, VND). First number after the
  line code = "Quý này" column:
      code 10 Doanh thu thuần          -> Net revenue (VNDbn)
      code 20 Lợi nhuận gộp            -> Gross profit (VNDbn)
  Checked: 10 - 11 (giá vốn) = 20.
- Segment note "Bộ phận theo lĩnh vực kinh doanh" (note VIII.5, 2nd-3rd last page, in USD).
  Fixed row order: thương mại / dịch vụ khoan / kỹ thuật giếng khoan & dịch vụ khác / Tổng;
  columns: Tài sản, Nợ phải trả, Doanh thu, Lợi nhuận gộp for this quarter, then the same for
  last year's quarter. Revenue + GP -> Trading / Drilling / Well services net revenue + GP.
  USD -> VNDbn at the implied rate VND net revenue / USD segment revenue total (the same
  conversion the Diamond history uses: 1Q2026 matches to 0.01bn, 2Q2026 within 0.15bn).
  Checked: segment sums = note total, note GP total x rate = P&L GP.
  Well services = "kỹ thuật giếng khoan và dịch vụ các loại khác" (incl. other services).

Not sourceable from PVD disclosures (left out):
- Jack-up day rate (USD/day): analyst estimate (Diamond "Schedule" sheet); neither the FS, the
  explanatory letter ("Giải trình BCTC") nor the rig report gives day rates.
- Jack-up utilisation (%): the quarterly "Báo cáo hoạt động Giàn khoan" press item is a
  contract-schedule infographic (status/contract dates per rig), no utilisation figure. The
  Giải trình letter gives average owned jack-ups / chartered rigs in operation (e.g. 5.91 / 2.59)
  - a possible new column, not this one.

Caveats: OCR mixes '.' and ',' as thousand separators ("3.027.879,700.914") -> _amount() treats
every 3-digit group as thousands (all amounts here are integers). OCR text per page is cached
next to the PDF (common.ocr_pages) - OCR is ~10-20 s/page on a laptop CPU.
"""

import re

from functools import partial

from disclosures.common import Result, fetch_bctc, site_bctc, page_text, pdf_pages, to_bn

TICKER = "PVD"
# the company IR list - raced against CafeF / Vietstock by common.fetch_bctc
WEBSITE = partial(site_bctc, "https://www.pvdrilling.com.vn/quan-he-co-dong/bao-cao-tai-chinh")
SEGMENTS = [  # segment note rows, in order; patterns only sanity-check the OCR'd labels
    ("Trading", r"th\w?\W?\w{0,3}ng\s*m"),
    ("Drilling", r"^(?!.*gi[eế]ng).*(khoan|d[iị]ch\s*v[uụ])"),  # label may wrap: "Bộ phận cung cấp dịch vụ / khoan"
    ("Well services", r"gi[eế]ng|thu\W?[aậ]t"),
]
AMOUNT = r"\(?-?\d{1,3}(?:[.,]\s?\d{3})+\)?"


def _amount(text):
    """'3.027.879,700.914' / '(1,213,471,237)' -> int (OCR mixes '.' and ',' as thousand separators)."""
    neg = text.startswith("(") or text.startswith("-")
    v = int(re.sub(r"\D", "", text))
    return -v if neg else v


def _amounts(line):
    return [_amount(m) for m in re.findall(AMOUNT, line)]


def _ocr(path, page):
    return page_text(path, page)  # text layer, else full-resolution OCR (cached)


def _pnl(path, res):
    """{code: this-quarter VND} for codes 10, 11, 20 from the consolidated P&L page."""
    for page in (5, 4, 6, 3):
        text = _ocr(path, page)
        if not re.search(r"KET\s*QUA\s*HOAT\s*D[OQ]NG\s*KINH\s*DOANH", text, re.I):
            continue
        out = {}
        for line in text.splitlines():
            # B02 item number (3. / 4. / 5.) or line code (10 / 11 / 20) - OCR sometimes drops one of them
            m = re.match(r"\s*([345])\s*[.,]", line)
            code = {"3": "10", "4": "11", "5": "20"}[m.group(1)] if m else None
            m = re.search(r"\(\s*\d+\s*=.*?\)\s*(\d{2})\s|\s(1[01]|20)\s", line)
            code = code or (m and (m.group(1) or m.group(2)))
            nums = _amounts(line)
            if code in ("10", "11", "20") and code not in out and nums:
                out[code] = nums[0]
        if out:
            return page, out
    res.warn("P&L page not found (pages 3-6) - layout changed?")
    return None, {}


def _segments(path, res):
    """[(segment, revenue USD, GP USD)] + (total revenue, total GP) from the business segment note."""
    n = pdf_pages(path)
    for page in (n - 1, n - 2, n, n - 3, n - 4):
        text = _ocr(path, page)
        m = re.search(r"b[oộ]\s*ph[aậ]n\s*th\w{0,2}o\s*l\w{0,2}nh\s*v\w{0,3}c\s*kinh\s*doanh", text, re.I)
        if not m:
            continue
        rows, label = [], ""
        for line in text[m.end():].splitlines():
            nums = _amounts(line)
            if len(nums) < 6:
                label += " " + line  # label wrapped over several lines
                continue
            rows.append((label + " " + line, nums))
            label = ""
            if re.search(r"t[oổ]ng\s*c[oộ]ng", line, re.I):
                break
        if len(rows) < 4:
            continue
        out = []
        for (seg, pattern), (lab, nums) in zip(SEGMENTS, rows):
            if not re.search(pattern, lab, re.I):
                res.warn(f"segment row '{lab.strip()[:50]}' does not look like {seg} - row order changed?")
            if len(nums) != 8:
                res.warn(f"{seg}: {len(nums)} numbers on the row (8 expected) - check OCR")
            out.append((seg, nums[2], nums[3]))
        total = rows[3][1]
        return page, out, (total[2], total[3])
    res.warn("segment note (Bộ phận theo lĩnh vực kinh doanh) not found in the last pages")
    return None, [], (None, None)


def _close(a, b, tol=0.01):
    return a is not None and b is not None and abs(a - b) <= tol * max(abs(b), 1)


def collect(q, y):
    res = Result()
    path, doc = fetch_bctc(TICKER, q, y, website=WEBSITE)
    if not path:
        res.warn(f"BCTC hop nhat Q{q}/{y} chua co tren CafeF/Vietstock/pvdrilling.com.vn (thuong ~30 ngay sau khi het quy)")
        return res
    res.sources.append(f"{doc['title']} ({doc['site']}) {doc['url']}")

    page, pnl = _pnl(path, res)
    nr, cogs, gp = (pnl.get(k) for k in ("10", "11", "20"))
    if nr is not None:
        res.values["Net revenue (VNDbn)"] = to_bn(nr)
    if gp is not None:
        res.values["Gross profit (VNDbn)"] = to_bn(gp)
    if None in (nr, cogs, gp) or not _close(nr - cogs, gp, 0.001):
        res.warn(f"P&L p.{page}: revenue {nr} - COGS {cogs} != GP {gp} - check OCR")

    page, segs, (tot_rev, tot_gp) = _segments(path, res)
    if not segs or nr is None or not tot_rev:
        return res
    if not _close(sum(r for _, r, _ in segs), tot_rev):
        res.warn(f"segment revenue sum {sum(r for _, r, _ in segs):,} != total {tot_rev:,} USD (p.{page})")
    if not _close(sum(g for _, _, g in segs), tot_gp):
        res.warn(f"segment GP sum {sum(g for _, _, g in segs):,} != total {tot_gp:,} USD (p.{page})")
    rate = nr / tot_rev  # VND per USD implied by the two statements
    if not 20000 < rate < 32000:
        res.warn(f"implied VND/USD {rate:,.0f} implausible - segment note may be in VND or misread")
        return res
    if gp is not None and not _close(tot_gp * rate, gp):
        res.warn(f"segment GP total {to_bn(tot_gp * rate):,.1f} bn != P&L GP {to_bn(gp):,.1f} bn")
    for seg, rev, seg_gp in segs:
        res.values[f"{seg} net revenue (VNDbn)"] = to_bn(rev * rate)
        res.values[f"{seg} gross profit (VNDbn)"] = to_bn(seg_gp * rate)
    res.warn(f"segments are in USD (note p.{page}); converted at implied {rate:,.0f} VND/USD")
    return res
