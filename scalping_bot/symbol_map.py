"""
scalping_bot/symbol_map.py — แปลงสัญลักษณ์ → ชื่อจริงในโบรก (รองรับทุกชนิดบัญชี)

ชื่อสัญลักษณ์ต่างกันตามชนิดบัญชี Exness: Standard=ไม่มี suffix · บางชนิด='m'/'c'/'z'
→ resolve() ลองชื่อตรง + suffix ที่โบรกนิยม แล้วเช็กกับรายชื่อจริง = ใช้ได้ทั้ง Demo/Real
รับได้ทั้งชื่อ core (XAUUSD) และชื่อมี suffix (XAUUSDm) — ตัด suffix ออกก่อน resolve
"""
from __future__ import annotations
from typing import Optional

_SUFFIXES = ("", "m", "c", "z", "pro", ".raw", "_raw")

# commodity: keyword → ชื่อ core
_COMMO = (
    (("XAU", "GOLD", "GC=F", "ทอง"), "XAUUSD"),
    (("XAG", "SILVER", "SI=F", "เงิน"), "XAGUSD"),
    (("CL=F", "WTI", "USOIL", "น้ำมัน", "OIL"), "USOIL"),
    (("BZ=F", "BRENT", "UKOIL"), "UKOIL"),
)


def _core(sym: str) -> str:
    """ตัด suffix ที่รู้จักออก เพื่อให้เหลือชื่อ core (XAUUSDm → XAUUSD)"""
    s = sym.upper().lstrip("$").replace(".BK", "")
    for suf in ("M", "C", "Z"):
        if len(s) > 6 and s.endswith(suf):
            return s[:-1]
    return s


def resolve(symbol: str, broker_symbols: set[str]) -> Optional[str]:
    """หาชื่อจริงในโบรกจากชื่อ core/มี-suffix — คืน None ถ้าโบรกไม่มี"""
    core = _core(symbol)
    for suf in _SUFFIXES:
        cand = core + suf
        if cand in broker_symbols:
            return cand
    # เผื่อ broker เก็บเป็นพิมพ์เล็ก/ต่าง — เทียบ core ตรง ๆ
    for s in broker_symbols:
        if _core(s) == core:
            return s
    return None


def map_symbol(sig: dict, broker_symbols: set[str]) -> Optional[str]:
    market = sig.get("market", "")
    sym = (sig.get("symbol") or "").upper()
    disp = (sig.get("display") or "").upper()

    if market == "us":
        return resolve(sym, broker_symbols)
    if market == "crypto":
        base = sym.split("/")[0].replace("USDT", "").replace("USD", "")
        return resolve(base + "USD", broker_symbols) or resolve(base + "USDT", broker_symbols)
    if market == "commodity":
        for keys, core in _COMMO:
            if any(k in sym or k in disp for k in keys):
                return resolve(core, broker_symbols)
    return None
