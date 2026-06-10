"""
part2_mt5/market_hours.py — เวลาเปิด-ปิดตลาดโดยประมาณ (เวลาไทย UTC+7)

ใช้สำหรับฟีเจอร์ "TP ก่อนตลาดปิด" และ filter "ไม่สแกนตลาดปิด"
ส่วนคริปโตเปิด 24 ชม. ไม่มีวันปิด → ใช้ SL/TP ปกติ

*** MT5 Python ไม่มี session API → ใช้เวลามาตรฐาน (อาจคลาด ±1 ชม.ตาม DST) — ปรับ constant ได้ ***
เวลา (โดยประมาณ ช่วง summer/EDT UTC-4):
  • หุ้น+ดัชนี US: เปิด 9:30 ET ≈ 20:30 ไทย  |  ปิด 16:00 ET ≈ 03:00 ไทย (winter: เปิด 21:30)
  • โลหะ/พลังงาน/FX/ดัชนีอื่น: ~24/5 ปิดจริงแค่สุดสัปดาห์ ศุกร์ 17:00 ET ≈ เสาร์ 04:00 ไทย
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

# UTC+7 (ไทย) — explicit เพื่อให้ถูกต้องบน VPS ทุก timezone
_TZ_THAI = timezone(timedelta(hours=7))

_CRYPTO = ("BTC", "XBT",  # Bitcoin (MT5 standard + Kraken-style)
           "ETH", "XRP", "LTC", "BCH", "SOL", "ADA", "DOGE", "BNB", "DOT",
           "LINK", "XLM", "TRX", "SHIB", "AVAX", "MATIC", "UNI", "ATOM", "NEAR", "APT")
_US_INDEX = ("US30", "US500", "USTEC", "NAS", "SPX", "USTECH", "USIDX")
_COMMOD = ("XAU", "XAG", "XPT", "XPD", "XCU", "USOIL", "UKOIL", "XNG", "XBR", "XTI", "XNGUSD")
_EU_ASIA_IDX = ("DE30", "DE40", "UK100", "JP225", "HK50", "AUS200", "STOXX", "FRA40", "EU50", "ES35", "CN50")
_CCY = ("USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "SGD", "HKD",
        "CNH", "SEK", "NOK", "DKK", "ZAR", "MXN", "TRY", "PLN", "CZK", "HUF")

# เวลาเปิด-ปิด (เวลาไทย UTC+7) — แก้ได้ถ้า DST เปลี่ยน
US_OPEN_H,  US_OPEN_M  = 20, 30      # ตลาดหุ้น US เปิด ~20:30 ไทย (9:30 AM EDT summer; winter=21:30)
US_CLOSE_H, US_CLOSE_M = 3,  0       # ตลาดหุ้น US ปิด ~03:00 ไทย (4:00 PM ET)
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


def is_open(sym: str, now: datetime = None) -> bool:
    """ตลาดของ sym เปิดอยู่ตอนนี้ไหม (เวลาไทย UTC+7)

    crypto      → True เสมอ (24/7)
    FX/commodity/index  → True วันจันทร์-เสาร์เช้า (24/5)
    หุ้น/ดัชนี US → เฉพาะ ~20:30–03:00 ไทย วันจันทร์-ศุกร์
    """
    if is_24h(sym):
        return True
    now = now or datetime.now(_TZ_THAI)
    cat = category(sym)
    wd = now.weekday()  # Mon=0 … Sun=6

    # อาทิตย์ทั้งวัน → ปิดทุกตลาด
    if wd == 6:
        return False

    if cat in ("us_stock", "us_index"):
        t_close = _at(now, US_CLOSE_H, US_CLOSE_M)   # 03:00 ไทย
        t_open  = _at(now, US_OPEN_H,  US_OPEN_M)    # 20:30 ไทย
        if now < t_close:
            # 00:00–03:00: ยังอยู่ในเซสชัน ET วันก่อน → เปิดถ้าเมื่อวานเป็นวันทำการ (อ–ส, wd 1–5)
            return 1 <= wd <= 5
        if now >= t_open:
            # 20:30–23:59: เปิด US session → เปิดถ้าวันนี้เป็นวันทำการ (จ–ศ, wd 0–4)
            return wd <= 4
        # 03:00–20:30: ช่วงกลางวันไทย → ตลาด US ปิด
        return False

    # FX / commodity / EU+Asia index: 24/5
    # เสาร์หลัง WEEKEND_CLOSE (04:00) = ปิดสนิท
    if wd == 5:
        return now < _at(now, WEEKEND_CLOSE_H, WEEKEND_CLOSE_M)
    return True


def in_volatile_window(sym: str, open_range_min: int = 30, close_range_min: int = 15,
                       now: datetime = None) -> bool:
    """US stocks/indices: True ถ้าอยู่ในช่วง opening range หรือ closing range

    Opening range : N นาทีแรกหลัง US market เปิด (20:30 → 20:30+N ไทย)
                    ช่วงนี้ volatility พุ่ง สัญญาณ VWAP/EMA มักเป็น false signal
    Closing range : N นาทีก่อน US market ปิด (03:00-N → 03:00 ไทย)
                    window dressing + stop hunts ทำให้ SL โดนเขี่ย

    crypto/FX/commodity → False เสมอ (ไม่มี hard session open/close)
    """
    if is_24h(sym):
        return False
    cat = category(sym)
    if cat not in ("us_stock", "us_index"):
        return False
    now = now or datetime.now(_TZ_THAI)
    wd = now.weekday()
    if wd == 6:
        return False
    t_open  = _at(now, US_OPEN_H,  US_OPEN_M)      # 20:30 ไทย
    t_close = _at(now, US_CLOSE_H, US_CLOSE_M)     # 03:00 ไทย
    # Opening range: 20:30 → 20:30 + open_range_min
    if t_open <= now < t_open + timedelta(minutes=open_range_min):
        return True
    # Closing range: 03:00 - close_range_min → 03:00
    if t_close - timedelta(minutes=close_range_min) <= now < t_close:
        return True
    return False


def in_us_session(buffer_min: int = 0, now: datetime = None) -> bool:
    """True ถ้าตอนนี้อยู่ในช่วงตลาดหุ้น US เปิด รวม buffer ก่อนเปิด (เวลาไทย)
    เช่น buffer_min=30 → True ตั้งแต่ 20:00 ไทย (ก่อนเปิด 20:30) ถึง 03:00 ไทย
    ใช้กับ FX session mode: เทรด FX เฉพาะนอกช่วงนี้"""
    now = now or datetime.now(_TZ_THAI)
    wd = now.weekday()
    t_open = _at(now, US_OPEN_H, US_OPEN_M) - timedelta(minutes=buffer_min)
    t_close = _at(now, US_CLOSE_H, US_CLOSE_M)
    if now < t_close:       # 00:00–03:00: session ET ของเมื่อวาน → อ-ส (wd 1-5)
        return 1 <= wd <= 5
    if now >= t_open:       # 20:00+ (รวม buffer): session วันนี้ → จ-ศ (wd 0-4)
        return wd <= 4
    return False


def closing_soon(sym: str, buffer_min: int, now: datetime = None) -> bool:
    """ตลาดของ sym ใกล้ปิดภายใน buffer_min นาทีไหม (เวลาไทย) — crypto → False เสมอ"""
    if is_24h(sym):
        return False
    # ใช้ Thai timezone explicit เพื่อให้ถูกต้องบน VPS ทุก timezone (ไม่พึ่ง OS local time)
    now = now or datetime.now(_TZ_THAI)
    nxt = _next_close(category(sym), now)
    if nxt is None:
        return False
    mins = (nxt - now).total_seconds() / 60.0
    return 0 < mins <= buffer_min
