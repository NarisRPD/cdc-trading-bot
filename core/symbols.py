"""
core/symbols.py — แปลง symbol ที่ผู้ใช้พิมพ์ → (market, data_ticker, display)

ผู้ใช้พิมพ์สั้น ๆ ได้:
  SOL          → crypto  SOL/USDT
  SOL/USDT     → crypto  SOL/USDT
  AAPL         → us      AAPL
  CPALL        → thai    CPALL.BK
  CPALL.BK     → thai    CPALL.BK
  XAUUSD       → commodity GC=F   (ทอง)
  XAGUSD       → commodity SI=F   (เงิน)
  XCUUSD       → commodity HG=F   (ทองแดง)
  GC=F         → commodity GC=F

ลำดับการเดา: commodity alias → .BK → มี '/' → อยู่ใน SET100 → อยู่ใน S&P500 → ที่เหลือเป็น crypto
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

from universe.set100 import get_set100_symbols
from universe.sp500 import get_sp500_static
from universe.nasdaq100 import get_nasdaq100_static
from universe.sp600 import get_sp600_static

Market = Literal["crypto", "us", "thai", "commodity"]

# alias โลหะ (forex-style) → yahoo ticker + ชื่อโชว์
_COMMODITY_ALIAS: dict[str, tuple[str, str]] = {
    "XAUUSD": ("GC=F", "Gold (XAUUSD)"),
    "XAGUSD": ("SI=F", "Silver (XAGUSD)"),
    "XCUUSD": ("HG=F", "Copper (XCUUSD)"),
    # รับ yahoo ticker ตรง ๆ ด้วย
    "GC=F": ("GC=F", "Gold (GC=F)"),
    "SI=F": ("SI=F", "Silver (SI=F)"),
    "HG=F": ("HG=F", "Copper (HG=F)"),
}

# cache membership sets (สร้างครั้งเดียวตอน import)
_SET100 = {s.upper() for s in get_set100_symbols()}
# US = S&P500 + NASDAQ-100 + S&P600 (ไว้เดาว่า symbol เป็นหุ้น US)
_SP500 = {
    s.upper()
    for s in (get_sp500_static() + get_nasdaq100_static() + get_sp600_static())
}


@dataclass(frozen=True)
class Resolved:
    market: Market
    data_ticker: str   # ticker จริงสำหรับดึงข้อมูล (SOL/USDT, AAPL, CPALL.BK, GC=F)
    display: str       # ชื่อโชว์ในข้อความ


def resolve_symbol(raw: str) -> Resolved:
    """แปลง input ผู้ใช้ → Resolved (เดา market ให้). raise ValueError ถ้าว่าง"""
    s = (raw or "").strip().upper()
    if not s:
        raise ValueError("empty symbol")

    # 1) โลหะ (alias หรือ yahoo ticker)
    if s in _COMMODITY_ALIAS:
        ticker, disp = _COMMODITY_ALIAS[s]
        return Resolved("commodity", ticker, disp)

    # 2) ระบุ .BK ชัดเจน → หุ้นไทย
    if s.endswith(".BK"):
        return Resolved("thai", s, s[:-3])

    # 3) มี '/' → crypto pair เต็ม
    if "/" in s:
        return Resolved("crypto", s, s)

    # 4) อยู่ใน SET100 → หุ้นไทย
    if s in _SET100:
        return Resolved("thai", f"{s}.BK", s)

    # 5) อยู่ใน S&P500 → หุ้น US
    if s in _SP500:
        return Resolved("us", s, s)

    # 6) ที่เหลือถือเป็น crypto → เติม /USDT
    return Resolved("crypto", f"{s}/USDT", f"{s}/USDT")
