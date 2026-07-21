"""
universe/sector_map.py — ticker → เซกเตอร์ (GICS) สำหรับกระจายความเสี่ยงตอนจัดอันดับ

ทำไมต้องมี: rs_rank วัดความแข็งเทียบ "ทั้งตลาด" → เซกเตอร์ไหนกำลังนำ
หุ้นทั้งเซกเตอร์จะได้ RS สูงพร้อมกันแล้วกวาดอันดับไปทั้งหมด
ผลคือ Top 5 เป็นเซกเตอร์เดียวกันหมด = เดิมพันก้อนเดียว ไม่ใช่ 5 ก้อน
และตัวเลข "เสีย X% ถ้าโดน SL ครบ" จะต่ำกว่าจริง เพราะมันคิดบนสมมติฐานว่าแต่ละตัวเป็นอิสระ

แหล่งข้อมูล: คอลัมน์ GICS Sector ในหน้า Wikipedia เดียวกับที่ดึงรายชื่อหุ้นอยู่แล้ว
→ ใช้ cache ร่วมกับ fetch_wiki_tickers = ไม่มีค่าใช้จ่ายเน็ตเพิ่ม
ดึงไม่ได้ก็คืน {} แล้วระบบจัดอันดับทำงานต่อได้ตามปกติ (แค่ไม่มีการจำกัดเซกเตอร์)
"""
from __future__ import annotations
import logging
from typing import Optional

from universe._wiki import fetch_wiki_sectors

log = logging.getLogger(__name__)

_SOURCES = [
    ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "S&P500"),
    ("https://en.wikipedia.org/wiki/Nasdaq-100", "NASDAQ100"),
    ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", "S&P400"),
    ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", "S&P600"),
]

# GICS 11 เซกเตอร์ → ไทย (ให้ตรงกับชื่อที่ /sectors ใช้ จะได้อ่านคู่กันรู้เรื่อง)
_TH = {
    "information technology": "เทคโนโลยี",
    "communication services": "สื่อสาร/มีเดีย",
    "consumer discretionary": "สินค้าฟุ่มเฟือย",
    "consumer staples": "สินค้าจำเป็น",
    "financials": "การเงิน",
    "health care": "สุขภาพ",
    "healthcare": "สุขภาพ",
    "industrials": "อุตสาหกรรม",
    "energy": "พลังงาน",
    "materials": "วัตถุดิบ",
    "utilities": "สาธารณูปโภค",
    "real estate": "อสังหาฯ",
}

_CACHE: Optional[dict] = None


def sector_map() -> dict:
    """{ticker: ชื่อเซกเตอร์ภาษาไทย} รวมทั้ง 3 universe — คำนวณครั้งเดียวต่อโปรเซส"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    merged: dict = {}
    for url, name in _SOURCES:
        try:
            for tk, sec in fetch_wiki_sectors(url, name=name).items():
                merged.setdefault(tk, _TH.get(sec.strip().lower(), sec.strip()))
        except Exception as e:  # noqa: BLE001 — ไม่มี sector ก็ยังสแกนได้
            log.warning("sector map %s ล้มเหลว: %s", name, e)
    _CACHE = merged
    log.info("sector map: รวม %d ตัว", len(merged))
    return merged


def sector_of(ticker: str) -> Optional[str]:
    return sector_map().get((ticker or "").strip().upper())


def diversify(ranked: list, n: int, max_per_sector: int, sec_of) -> "tuple[list, dict]":
    """เลือก n ตัวจาก ranked (เรียงคะแนนแล้ว) โดยจำกัดจำนวนต่อเซกเตอร์

    ranked = list ของอะไรก็ได้ · sec_of(item) -> ชื่อเซกเตอร์ หรือ None
    เซกเตอร์ที่ไม่รู้ (None) ไม่ถูกจำกัด — ข้อมูลขาดไม่ควรกลายเป็นการลงโทษ
    คืน (ที่เลือก, {เซกเตอร์: จำนวน})  · max_per_sector <= 0 = ปิดการจำกัด
    """
    picked, used = [], {}
    if max_per_sector <= 0:
        picked = ranked[:n]
        for it in picked:
            s = sec_of(it)
            if s:
                used[s] = used.get(s, 0) + 1
        return picked, used

    overflow = []                      # ตัวที่ถูกกันไว้ — เผื่อได้ไม่ครบ n จะดึงกลับมา
    for it in ranked:
        if len(picked) >= n:
            break
        s = sec_of(it)
        if s and used.get(s, 0) >= max_per_sector:
            overflow.append(it)
            continue
        picked.append(it)
        if s:
            used[s] = used.get(s, 0) + 1
    # ตลาดแคบจนหาไม่ครบ → ยอมรับตัวที่เกินโควตาดีกว่าส่งไม่ครบจำนวน
    for it in overflow:
        if len(picked) >= n:
            break
        picked.append(it)
        s = sec_of(it)
        if s:
            used[s] = used.get(s, 0) + 1
    return picked, used
