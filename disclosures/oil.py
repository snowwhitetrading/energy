"""
PV OIL (OIL) - quarter figures from the quarterly consolidated financial statements.

Document: the quarterly consolidated BCTC ("Báo cáo tài chính hợp nhất quý X năm Y") from CafeF
or Vietstock, whichever downloads first (common.fetch_bctc). Published ~30 days after quarter
end. A scan (the text layer is the signature stamp) -> OCR of two pages only:

- P&L "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH HỢP NHẤT" (page ~5). First amount after the line
  code = "Kỳ này" (this quarter):
      code 10 Doanh thu thuần   -> Net revenue (VNDbn)
      code 20 Lợi nhuận gộp     -> Gross profit (VNDbn)
- Notes "DOANH THU THUẦN VỀ BÁN HÀNG VÀ CUNG CẤP DỊCH VỤ" and "GIÁ VỐN HÀNG BÁN VÀ DỊCH VỤ
  CUNG CẤP" (same page, ~0.8 into the report; note numbers move, rows are read by label):
      Services net revenue   = "Doanh thu cung cấp dịch vụ và hoạt động khác"
                               + "Doanh thu từ ủy thác xuất nhập khẩu dầu thô"
      Services gross profit  = that - "Giá vốn dịch vụ cung cấp"
      Inventory provision    = "Trích lập/(Hoàn nhập) dự phòng giảm giá hàng tồn kho"
                               (positive = charge, negative = reversal)
  Checked against the Diamond history: 1H2026 services revenue 254.51 + 117.54 = 372.05
  = 1Q 187.17 + 2Q 184.88; services GP 372.05 - 183.17 = 188.88 = 122.06 + 66.82.
  The notes are YEAR-TO-DATE -> quarter = this report - the previous quarter's (Q1 as is).
- Fuel trading net revenue / gross profit = company total - services (`derive`), so the two
  segments add up and the inventory provision and sales deductions sit in fuel trading.

Not in the financial statements (left out): Total Volume (k m3). Unit gross profit
(VND/litre) = GP / volume * 1000 is computed only when the CSV already holds the quarter's
volume (flagged).
"""

import re

from disclosures.common import (Result, fetch_bctc, locate_pages, numbers_in, page_text, pdf_pages,
                                prev_quarter, to_bn, _flat)

TICKER = "OIL"
NR, GP = "Net revenue (VNDbn)", "Gross profit (VNDbn)"
SRV_NR, SRV_GP = "Services net revenue (VNDbn)", "Services gross profit (VNDbn)"
FUEL_NR, FUEL_GP = "Fuel trading net revenue (VNDbn)", "Fuel trading gross profit (VNDbn)"
VOL, UNIT_GP, PROV = "Total Volume (k m3)", "Unit gross profit (VND/litre)", "Inventory provision (VNDbn)"

NOTE = r"doanh\s*thu\s*thuan\s*ve\s*ban\s*hang"
ROWS = {  # accent-free label -> key; first amount on the line = year to date
    "srv_rev": r"doanh\s*thu\s*cung\s*cap\s*d\w*ch\s*v\w*",
    "trust_rev": r"uy\s*thac\s*xuat\s*nhap\s*khau",
    "srv_cogs": r"gia\s*von\s*d\w*ch\s*v\w*\s*cung\s*cap",
    "provision": r"du\s*phong\s*giam\s*gia\s*hang\s*ton",
}


def _big(line):
    return [n for n in numbers_in(line) if n is not None and abs(n) >= 1e6]  # drop codes / note refs


def _pnl(path, res):
    """{"10": VND, "11": VND, "20": VND} this quarter from the consolidated P&L page."""
    for page in (5, 4, 6, 3, 7):
        text = page_text(path, page)
        if not re.search(r"ket\s*qua\s*hoat\s*dong\s*kinh\s*doanh", _flat(text)):
            continue
        out = {}
        for line in text.splitlines():
            t = _flat(line)
            for code, pat in (("10", r"doanh\s*thu\s*thuan"), ("11", r"gia\s*von"), ("20", r"loi\s*nhuan\s*gop")):
                nums = _big(line)
                if code not in out and re.search(pat, t) and nums:
                    out[code] = nums[0]
        if out:
            return page, out
    res.warn("P&L page not found (pages 3-7) - layout changed?")
    return None, {}


def _notes(path, res):
    """YTD VND of the ROWS keys from the revenue / COGS notes, or None."""
    pages = locate_pages(path, NOTE, start=pdf_pages(path) // 2, hint="OIL:revenue-note", first_only=True)
    if not pages:
        res.warn("revenue note (Doanh thu thuan ve ban hang ...) not found in the second half of the report")
        return None
    text = page_text(path, pages[0])
    if not re.search(r"gia\s*von\s*hang\s*ban", _flat(text)):
        text += "\n" + page_text(path, pages[0] + 1)  # COGS note spilled to the next page
    out = {}
    for line in text.splitlines():
        t = _flat(line)
        for key, pat in ROWS.items():
            nums = _big(line)
            if key not in out and re.search(pat, t) and nums:
                out[key] = nums[0]
    missing = [k for k in ("srv_rev", "srv_cogs") if k not in out]
    if missing:
        res.warn(f"p{pages[0]}: note rows not read {missing}")
    return out


def _read(q, y, res, need_pnl=True):
    path, doc = fetch_bctc(TICKER, q, y)
    if not path:
        return None, None
    res.sources.append(f"{doc['title']} ({doc['site']}) {doc['url']}")
    pnl = _pnl(path, res)[1] if need_pnl else {}
    return pnl, _notes(path, res)


def _csv_volume(q, y):
    from CongTyDauKhi import read_csv

    _, rows = read_csv(TICKER)
    return dict(rows).get(f"{q}Q{y}", {}).get(VOL)


def derive(res, q=None, y=None):
    """Columns computed from others - re-run by update_disclosure.py after FA_Quarterly
    replaces the headline: fuel trading = total - services, unit GP from the CSV volume."""
    v = res.values
    if NR in v and SRV_NR in v:
        v[FUEL_NR] = v[NR] - v[SRV_NR]
    if GP in v and SRV_GP in v:
        v[FUEL_GP] = v[GP] - v[SRV_GP]
    vol = v.get(VOL) or (q and _csv_volume(q, y))
    if GP in v and vol:
        v[UNIT_GP] = v[GP] / vol * 1000


def collect(q, y):
    res = Result()
    pnl, notes = _read(q, y, res)
    if pnl is None:
        res.warn(f"BCTC hop nhat Q{q}/{y} chua co tren CafeF/Vietstock (thuong ~30 ngay sau quy)")
        return res
    if "10" in pnl:
        res.values[NR] = to_bn(pnl["10"])
    if "20" in pnl:
        res.values[GP] = to_bn(pnl["20"])
    if all(k in pnl for k in ("10", "11", "20")) and abs(pnl["10"] - pnl["11"] - pnl["20"]) > 1e9:
        res.warn("P&L: doanh thu thuan - gia von != loi nhuan gop - check OCR")

    if notes:
        if q > 1:  # the notes are year to date
            pq, py = prev_quarter(q, y)
            _, prev = _read(pq, py, res, need_pnl=False)
            if not prev:
                res.warn(f"notes are YTD and the {pq}Q{py} report could not be read - segments left out")
                notes = None
            else:
                notes = {k: v - prev.get(k, 0) for k, v in notes.items()}
                res.warn(f"services / provision = YTD {q * 3}M - YTD {pq * 3}M ({pq}Q{py} report)")
    if notes:
        if "srv_rev" in notes:
            res.values[SRV_NR] = to_bn(notes["srv_rev"] + notes.get("trust_rev", 0))
            if "srv_cogs" in notes:
                res.values[SRV_GP] = to_bn(notes["srv_rev"] + notes.get("trust_rev", 0) - notes["srv_cogs"])
        res.values[PROV] = to_bn(notes.get("provision", 0))  # no row = no provision this period

    derive(res, q, y)
    if UNIT_GP in res.values and not res.values.get(VOL):
        res.warn(f"{UNIT_GP} uses the {VOL} already in the CSV, not a disclosure")
    return res
