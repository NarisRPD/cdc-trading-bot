"""
part2_mt5/market_hours.py — เวลาเปิด-ปิดตลาดโดยประมาณ (เวลาไทย UTC+7)

ใช้สำหรับฟีเจอร์ "TP ก่อนตลาดปิด": ตลาดหุ้น/โลหะ/FX/ดัชนี มีเวลาปิด (โดยเฉพาะสุดสัปดาห์)
ส่วนคริปโตเปิด 24 ชม. ไม่มีวันปิด → ใช้ SL/TP ปกติ

*** MT5 Python ไม่มี session API → ใช้เวลามาตรฐาน (อาจคลาด ±1 ชม.ตาม DST) — ปรับ constant ได้ ***
เวลาปิด (โดยประมาณ ช่วง summer/DST):
  • หุ้น+ดัชนี US (NVDA/AAPL/US500/USTEC/US30): 16:00 ET ≈ 03:00 ไทย (อังคาร-เสาร์เช้า)
  • โลหะ/พลังงาน/FX/ดัชนีอื่น: ~24/5 ปิดจริงแค่สุดสัปดาห์ ศุกร์ 17:00 ET ≈ เสาร์ 04:00 ไทย
"""
from __future__ import annotations
from datetime import datetime, timedelta

_CRYPTO = ("BTC", "ETH", "XRP", "LTC", "BCH", "SOL", "ADA", "DOGE", "BNB", "DOT",
           "LINK", "XLM", "TRX", "SHIB", "AVAX", "MATIC", "UNI", "ATOM", "NEAR", "APT")
_US_INDEX = ("US30", "US500", "USTEC", "NAS", "SPX", "USTECH", "USIDX")
_COMMOD = ("XAU", "XAG", "XPT", "XPD", "XCU", "USOIL", "UKOIL", "XNG", "XBR", "XTI", "XNGUSD")
_EU_ASIA_IDX = ("DE30", "DE40", "UK100", "JP225", "HK50", "AUS200", "STOXX", "FRA40", "EU50", "ES35", "CN50")
_CCY = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SGD", "HKD",
        "CNH", "SEK", "NOK", "DKK", "ZAR", "MXN", "TRY", "PLN", "CZK", "HUF")

# เวลาปิด (เวลาไทย) — แก้ได้ถ้า DST เปลี่ยน
US_CLOSE_H, US_CLOSE_M = 3, 0        # ตลาดหุ้น US ปิด ~03:00 ไทย
WEEKEND_CLOSE_H, WEEKEND_CLOSE_M = 4, 0   # ปิดสุดสัปดาห์ ~เสาร์ 04:00 ไทย


def is_24h(sym: str) -> bool:
    u = sym.upper()
    return any(u.startswith(c) for c in _CRYPTO)


def category(sym: str) -> str:
    u = sym.upper()
    if is_24h(sym):
        return "crypto"
    if any(u.startswith(c) for c in _US_INDEX):
        return "us_index"
    if any(u.startswith(c) for c in _COMMOD):
        return "commodity"
    if any(u.startswith(c) for c in _EU_ASIA_IDX):
        return "index"
    base = u[:-1] if (len(u) == 7 and u.endswith("M")) else u   # ตัด suffix โบรก (เช่น 'm')
    if len(base) == 6 and base[:3] in _CCY and base[3:] in _CCY:
        return "fx"            # คู่เงิน = รหัสสกุล 2 ตัวต่อกัน (EURUSD, GBPJPY...)
    return "us_stock"          # ดีฟอลต์: ticker ตัวอักษรล้วน = หุ้น US


def _at(now: datetime, hh: int, mm: int) -> datetime:
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _next_close(cat: str, now: datetime):
    """เวลาปิดตลาดครั้งถัดไป (เวลาไทย) ของหมวดนั้น — None ถ้า 24ชม./ตอนนี้ตลาดปิดอยู่แล้ว"""
    wd = now.weekday()         # Mon=0 .. Sat=5, Sun=6
    if cat in ("us_stock", "us_index"):
        # ปิด ~03:00 ไทย วันที่ตลาด US เปิด: จันทร์-ศุกร์ ET = อังคาร-เสาร์ไทยเช้า (wd 1-5)
        close = _at(now, US_CLOSE_H, US_CLOSE_M)
        if wd in (1, 2, 3, 4, 5) and now <= close:
            return close
        return None
    if cat in ("commodity", "fx", "index"):
        # ~24/5 → ปิดจริงแค่สุดสัปดาห์: เสาร์ไทยเช้า ~04:00
        if wd == 5:            # เสาร์
            close = _at(now, WEEKEND_CLOSE_H, WEEKEND_CLOSE_M)
            if now <= close:
                return close
        return None
    return None                # crypto


def correlation_group(sym: str) -> str:
    """กลุ่มสินทรัพย์ที่วิ่งสัมพันธ์กัน (ใช้กระจายความเสี่ยง — ไม่เปิดทับกลุ่มเดียวเยอะ)"""
    u = sym.upper()
    cat = category(sym)
    if cat in ("us_stock", "us_index", "index"):
        return "หุ้น/ดัชนี"        # หุ้น US + ดัชนี US + ดัชนีโลก รวมกลุ่มเดียว (activity คัดตัววิ่งเยอะเอง)
    if cat == "commodity":
        if any(u.startswith(c) for c in ("XAU", "XAG", "XPT", "XPD", "XCU")):
            return "โลหะ"          # ทอง/เงิน/แพลทินัม/แพลเลเดียม/ทองแดง รวมกลุ่มเดียว
        return "พลังงาน"
    if cat == "crypto":
        return "คริปโต"
    if cat == "fx":
        return "FX"
    return "อื่นๆ"


def closing_soon(sym: str, buffer_min: int, now: datetime = None) -> bool:
    """ตลาดของ sym ใกล้ปิดภายใน buffer_min นาทีไหม (เวลาไทย) — crypto → False เสมอ"""
    if is_24h(sym):
        return False
    now = now or datetime.now()
    nxt = _next_close(category(sym), now)
    if nxt is None:
        return False
    mins = (nxt - now).total_seconds() / 60.0
    return 0 < mins <= buffer_min
