"""
core/correlate.py — วัดความสัมพันธ์ราคาจริงระหว่างหุ้น (ใช้ df ที่รอบสแกนมีอยู่แล้ว)

ทำไมป้ายเซกเตอร์อย่างเดียวไม่พอ (เคสจริง): HCSG ป้าย GICS = "อุตสาหกรรม"
(ขายบริการทำความสะอาด/อาหาร) แต่ลูกค้าทั้งหมดคือสถานพยาบาล → ราคาวิ่งตาม
กลุ่ม healthcare — การกระจายด้วยป้ายจึงถูกหลอกได้ ต้องวัดจากราคาจริงซ้ำอีกชั้น

ข้อมูล = daily returns 60 แท่งจาก DataFrame ที่ fetch มาแล้ว → ไม่ยิงเน็ตเพิ่มเลย
pure pandas/numpy: ไม่มี I/O
"""
from __future__ import annotations
import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_N_BARS = 60          # ~3 เดือน — สั้นพอสะท้อนความสัมพันธ์ปัจจุบัน ยาวพอไม่ใช่ noise
_MIN_OVERLAP = 30     # แท่งตรงกันน้อยกว่านี้ = ตัดสินไม่ได้ → คืน None


def _returns(df: pd.DataFrame) -> Optional[pd.Series]:
    """daily returns ของ _N_BARS แท่งท้าย — index เป็นวัน (normalize) ไว้ align ข้ามหุ้น"""
    try:
        c = df["close"].astype(float).tail(_N_BARS + 1)
        r = c.pct_change().dropna()
        if len(r) < _MIN_OVERLAP:
            return None
        r.index = pd.DatetimeIndex([
            (pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo else pd.Timestamp(x)).normalize()
            for x in r.index
        ])
        return r[~r.index.duplicated(keep="last")]
    except Exception:  # noqa: BLE001
        return None


def corr(df_a: pd.DataFrame, df_b: pd.DataFrame) -> Optional[float]:
    """สหสัมพันธ์ daily returns ระหว่างหุ้น 2 ตัว — None ถ้าข้อมูลไม่พอ"""
    ra, rb = _returns(df_a), _returns(df_b)
    if ra is None or rb is None:
        return None
    common = ra.index.intersection(rb.index)
    if len(common) < _MIN_OVERLAP:
        return None
    try:
        v = float(ra.loc[common].corr(rb.loc[common]))
    except Exception:  # noqa: BLE001
        return None
    return v if pd.notna(v) else None


def too_correlated(sym: str, picked_syms: list, items: dict, threshold: float) -> Optional[str]:
    """sym วิ่งตามตัวที่เลือกไปแล้วตัวไหนเกิน threshold ไหม — คืนชื่อตัวนั้น หรือ None

    ข้อมูลขาด (df ไม่มี/แท่งไม่พอ) = ตัดสินไม่ได้ → ปล่อยผ่าน (ไม่ลงโทษข้อมูลขาด)
    """
    df_a = (items or {}).get(sym)
    if df_a is None or getattr(df_a, "empty", True):
        return None
    for other in picked_syms:
        df_b = items.get(other)
        if df_b is None or getattr(df_b, "empty", True):
            continue
        c = corr(df_a, df_b)
        if c is not None and c >= threshold:
            return other
    return None
