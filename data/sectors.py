"""
data/sectors.py — Sector rotation (J): ดูว่าเงินไหลเข้า/ออกกลุ่มไหน

ใช้ sector ETF 11 ตัวของ S&P (SPDR) เทียบโมเมนตัมกับ SPY แทนการ map sector รายหุ้น
(ถูก + เร็ว — ดึงแค่ ~12 symbol, ไม่ชน rate limit) เป็นวิธีมาตรฐานวัด rotation
"""
from __future__ import annotations
import logging
from typing import Optional

log = logging.getLogger(__name__)

# SPDR sector ETFs + ชื่อไทย
_SECTOR_ETFS = {
    "XLK": "เทคโนโลยี",
    "XLC": "สื่อสาร/มีเดีย",
    "XLY": "สินค้าฟุ่มเฟือย",
    "XLF": "การเงิน",
    "XLV": "สุขภาพ",
    "XLI": "อุตสาหกรรม",
    "XLE": "พลังงาน",
    "XLB": "วัตถุดิบ",
    "XLP": "สินค้าจำเป็น",
    "XLU": "สาธารณูปโภค",
    "XLRE": "อสังหาฯ",
}


def _ret(df, n: int) -> Optional[float]:
    """ผลตอบแทน % ย้อนหลัง n แท่ง"""
    try:
        c = df["close"].astype(float)
        if len(c) <= n:
            return None
        return (c.iloc[-1] / c.iloc[-1 - n] - 1.0) * 100.0
    except Exception:  # noqa: BLE001
        return None


def sector_rotation() -> list[dict]:
    """คืน list เรียงตามโมเมนตัม 1 เดือน (มาก→น้อย):
      [{etf, name, ret_1m, ret_3m, rs_1m}]  (rs_1m = ผลตอบแทน 1m เทียบ SPY)
    คืน [] ถ้าดึงข้อมูลไม่ได้"""
    from data.quote import fetch_history
    spy = fetch_history("us", "SPY")
    spy_1m = _ret(spy, 21) if spy is not None else None
    rows: list[dict] = []
    for etf, name in _SECTOR_ETFS.items():
        df = fetch_history("us", etf)
        if df is None or df.empty:
            continue
        r1, r3 = _ret(df, 21), _ret(df, 63)
        rs1 = (r1 - spy_1m) if (r1 is not None and spy_1m is not None) else None
        rows.append({"etf": etf, "name": name, "ret_1m": r1, "ret_3m": r3, "rs_1m": rs1})
    rows.sort(key=lambda x: (x["ret_1m"] if x["ret_1m"] is not None else -1e9), reverse=True)
    return rows
