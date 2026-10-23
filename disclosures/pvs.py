"""
PTSC (PVS) - quarter figures from the quarterly consolidated financial statements.

Document: the quarterly consolidated BCTC from CafeF, Vietstock or ptsc.com.vn (WEBSITE), whichever downloads first
(common.fetch_bctc) - CafeF/HNX ship PTSC's English edition, ptsc.com.vn a bilingual one, so the
P&L labels below match both languages. Published ~28-29 days after quarter end (Q1 late Apr, Q2 ~29 Jul,
Q3 ~22 Oct, Q4 ~28 Jan). Not used: the separate ("Công ty mẹ") report, the reviewed half-year
("bán niên sau soát xét", mid/late Aug), the audited FY ("năm ... đã kiểm toán", late Mar) and
"Bản tin tài chính Quý X" - the bulletin only gives company-level 6M/quarter totals in charts
(no segments), so it adds nothing the FS lacks.

The PDF is a scan -> OCR (common.page_text / ocr_pages, RapidOCR), only the pages needed,
cached next to the PDF.

- P&L "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH HỢP NHẤT" / "CONSOLIDATED INCOME STATEMENT"
  (page ~5-8, VND). Columns:
  this quarter, last year's quarter, YTD, last year's YTD -> first amount on the line:
      Doanh thu thuần (code 10)                        -> Net revenue (VNDbn)
      Lợi nhuận gộp (code 20)                          -> Gross profit (VNDbn)
      Phần lãi/lỗ trong công ty liên doanh, liên kết   -> JV & associates income (VNDbn)
  Checked: doanh thu thuần - giá vốn = lợi nhuận gộp. Matched by label, not line code (OCR
  drops codes now and then).
- Segment note "Business activity and Geographical segment" (English note 27, the segment P&L
  table on page ~n-10 of n, VND million). Columns in fixed order: Supply of petroleum technology
  service vessels (OSV) / FSO-FPSO / Seismic, geo survey & subsea ROV / Supply base / Mechanical
  and construction (M&C) / Maintenance, repair & installment (O&M) / Other services / Total.
  Rows used: "Net revenue from external sales" -> <seg> net revenue, "Gross segment profit" ->
  <seg> gross profit ("Net cost from external sales" kept as a cross-check: rev - cost = GP).
  VND mn -> VNDbn = /1000.

YTD: the segment note is CUMULATIVE (Q2 = 6M, Q3 = 9M, Q4 = 12M) while the P&L has a quarter
column. For Q2-Q4 the segment figures are this report minus the previous quarterly report's
(the same table, cumulative to the previous quarter). Which it is gets checked, not assumed:
the external-sales total is compared with the P&L YTD and quarter net revenue.

Not used: segment "internal sales" (eliminated on consolidation - the CSV is external sales,
matching the Diamond history to <0.05%), segment assets/liabilities.

Caveats: OCR mixes '.' and ',' as thousand separators and sometimes doubles one ("1,285,.242")
-> _amount() drops every separator (all amounts are integers). Validation: segment sums vs the
Total column (>1% -> REVIEW, the cell is then derived from the other two rows when they agree),
segment totals vs P&L. OCR ~1 min/page on a busy machine; a cold run reads ~3-6 pages per report.
"""

import re

from functools import partial

from disclosures.common import Result, fetch_bctc, site_bctc, ocr_pages, page_text, pdf_pages, prev_quarter, to_bn

TICKER = "PVS"
# the company IR list - raced against CafeF / Vietstock by common.fetch_bctc
WEBSITE = partial(site_bctc, "https://www.ptsc.com.vn/co-dong/danh-cho-co-dong/bao-cao-tai-chinh")
SEGMENTS = ["OSV", "FSO/FPSO", "ROV & geophysical", "Supply base & ports", "M&C", "O&M", "Others"]
AMOUNT = r"(?<![\d.,])(?:\(?-?\d{1,3}(?:[.,]{1,2}\s?\d{2,3})+\)?|\(?-?\d{1,3}\)?(?=\s|$))"
PNL = {  # CSV column -> label on the P&L line, Vietnamese | English (OCR text, accents mostly lost)
    "Net revenue (VNDbn)": r"doanh\s*thu\s*thu\S*n|net\s*revenue",
    "Gross profit (VNDbn)": r"nhu\S*n\s*g\S*p|gross\s*profit",
    "JV & associates income (VNDbn)": (r"cong\s*ty\s*l\S*n\s*doanh|l\S*n\s*doanh,?\s*l\S*n\s*k"
                                       r"|joint\s*ventures?\s*(and|,)\s*associates|share\s*of\s*(net\s*)?(profit|loss)"),
}
COGS = r"gi\S*\s*v\S*n\s*h[aà]ng|cost\s*of\s*(sales|goods|services)"
PNL_TITLE = r"KET\s*QUA\s*HOAT\s*D[OQ]NG\s*KINH\s*DOANH\s*HOP\s*NHAT|CONSOLIDATED\s*INCOME\s*STATEMENT"


def _amount(text):
    """'1,285,.242' / '(3.239)' -> int (OCR mixes '.' and ',' as thousand separators)."""
    neg = text.startswith("(") or text.startswith("-")
    v = int(re.sub(r"\D", "", text))
    return -v if neg else v


def _amounts(line):
    return [_amount(m) for m in re.findall(AMOUNT, line)]


def _ocr(path, page, zoom=None):
    """Page text (text layer, else OCR - cached); `zoom` forces a sharper OCR pass."""
    return ocr_pages(path, [page], zoom=zoom)[page] if zoom else page_text(path, page)


def _pnl(path, res):
    """{CSV column: (quarter VND, YTD VND)} from the consolidated P&L page."""
    for page in (6, 5, 7, 4, 8):
        text = _ocr(path, page)
        if not re.search(PNL_TITLE, text, re.I):
            continue
        out, pending = {}, []
        for line in text.splitlines():
            nums = [n for n in _amounts(line) if abs(n) >= 1_000_000]  # drop line codes / note refs
            hits = [col for col, pat in list(PNL.items()) + [("cogs", COGS)]
                    if col not in out and re.search(pat, line, re.I)]
            if len(nums) < 3:
                pending = hits or pending  # label wrapped: its amounts are on the next line
                continue
            for col in hits or pending:
                out.setdefault(col, (nums[0], nums[2]))
            pending = []
        if out:
            return page, out
    res.warn("P&L page not found (pages 4-8) - layout changed?")
    return None, {}


def _row(text, pattern):
    """8 amounts (7 segments + total, VND mn) on the first line matching `pattern`, else None."""
    for line in text.splitlines():
        if re.search(pattern, line, re.I):
            nums = _amounts(line)
            return nums[-8:] if len(nums) >= 8 else None
    return None


def _segment_table(text):
    rows = {"rev": _row(text, r"revenue\s*from\s*external"),
            "cost": _row(text, r"cost\s*from\s*external"),
            "gp": _row(text, r"gross\s*segment\s*profit")}
    ok = {k: v is not None and abs(sum(v[:7]) - v[7]) <= max(3, abs(v[7]) * 0.001) for k, v in rows.items()}
    return rows, ok


def _segments(path, res):
    """{'rev': [7 segs + total], 'gp': [...]} in VND mn from the English segment P&L table."""
    n = pdf_pages(path)
    for page in (n - 10, n - 11, n - 9, n - 12, n - 8, n - 13, n - 7, n - 14, n - 6):
        text = _ocr(path, page)
        if not re.search(r"gross\s*segment\s*profit", text, re.I):
            continue
        rows, ok = _segment_table(text)
        if not all(ok.values()):  # retry sharper before patching
            text = _ocr(path, page, zoom=4.0)
            rows2, ok2 = _segment_table(text)
            if sum(ok2.values()) >= sum(ok.values()):
                rows, ok = rows2, ok2
        rev, cost, gp = rows["rev"], rows["cost"], rows["gp"]
        if not ok["gp"] and ok["rev"] and ok["cost"]:
            gp = [r - c for r, c in zip(rev, cost)]
            res.warn(f"p{page}: segment GP row unreadable/off - derived as external revenue - external cost")
        elif not ok["rev"] and ok["cost"] and ok["gp"]:
            rev = [c + g for c, g in zip(cost, gp)]
            res.warn(f"p{page}: segment revenue row unreadable/off - derived as external cost + GP")
        elif not (ok["rev"] and ok["gp"]):
            res.warn(f"p{page}: segment rows do not add up to the Total column (rows ok: {ok}) - skipped")
            return page, None
        return page, {"rev": rev, "gp": gp}
    res.warn("segment P&L table ('Gross segment profit') not found near the end - layout changed?")
    return None, None


def _read(q, y, res, need_pnl=True):
    """(pnl, segments) of the quarterly consolidated FS of q/y, or (None, None) if not out."""
    path, doc = fetch_bctc(TICKER, q, y, website=WEBSITE)
    if not path:
        return None, None
    res.sources.append(f"{doc['title']} ({doc['site']}) {doc['url']}")
    pnl = _pnl(path, res)[1] if need_pnl else {}
    page, seg = _segments(path, res)
    return pnl, seg


def collect(q, y):
    res = Result()
    pnl, seg = _read(q, y, res)
    if pnl is None:
        res.warn(f"BCTC hop nhat Q{q}/{y} chua co tren CafeF/Vietstock/ptsc.com.vn - chua cong bo?")
        return res

    for col in PNL:
        if col in pnl:
            res.values[col] = round(to_bn(pnl[col][0]), 2)
    if "Net revenue (VNDbn)" in pnl and "cogs" in pnl and "Gross profit (VNDbn)" in pnl:
        diff = pnl["Net revenue (VNDbn)"][0] - pnl["cogs"][0] - pnl["Gross profit (VNDbn)"][0]
        if abs(diff) > 1e9:
            res.warn(f"P&L: net revenue - COGS - gross profit = {to_bn(diff):,.1f}bn (OCR?)")
    missing = [c for c in PNL if c not in pnl]
    if missing:
        res.warn(f"P&L lines not read: {missing}")
    if not seg:
        return res

    # cumulative or quarter? compare the external-sales total with the P&L columns
    nr = pnl.get("Net revenue (VNDbn)")
    cumulative = q > 1
    if nr:
        total = seg["rev"][7] * 1e6
        near = lambda a, b: abs(a - b) <= abs(b) * 0.005  # noqa: E731
        if near(total, nr[1]) and not near(total, nr[0]):
            cumulative = True
        elif near(total, nr[0]):
            cumulative = False
        else:
            res.warn(f"segment revenue total {total / 1e9:,.0f}bn matches neither P&L quarter "
                     f"{nr[0] / 1e9:,.0f}bn nor YTD {nr[1] / 1e9:,.0f}bn")
    if cumulative and q > 1:
        pq, py = prev_quarter(q, y)
        _, prev = _read(pq, py, res, need_pnl=False)
        if not prev:
            res.warn(f"segment note is YTD but the {pq}Q{py} report could not be read - segments left out")
            return res
        seg = {k: [a - b for a, b in zip(seg[k], prev[k])] for k in seg}
        res.warn(f"segments = YTD {q * 3}M minus YTD {pq * 3}M ({pq}Q{py} quarterly report)")

    for i, name in enumerate(SEGMENTS):
        res.values[f"{name} net revenue (VNDbn)"] = round(seg["rev"][i] / 1000, 2)
        res.values[f"{name} gross profit (VNDbn)"] = round(seg["gp"][i] / 1000, 2)

    # segment totals vs the P&L quarter figures
    for key, col in (("rev", "Net revenue (VNDbn)"), ("gp", "Gross profit (VNDbn)")):
        if col in res.values:
            s, t = sum(seg[key][:7]) / 1000, res.values[col]
            if abs(s - t) > abs(t) * 0.01:
                res.warn(f"sum of segment {key} {s:,.1f}bn vs P&L {col} {t:,.1f}bn (>1% off)")
    return res
