"""
part2_mt5/regime.py — Market Regime Detection (trend / range / unclear)

หลักการนักเทรดมืออาชีพ: "Avoid trading in the middle of a range"
กลยุทธ์ตามเทรนด์ (utbot/hybrid/scalp/ema_m5) แพ้ในตลาด sideways —
คืนที่ M15 ขัด H1 ทั้งคืนคือ range day ที่บอทพยายามเข้าซ้ำๆ โดยไม่มี edge

ตรวจ 3 มุมอิสระบน H1 (default):
  1. ADX(14)             — แรงเทรนด์ (ต่ำ = ไม่มีเทรนด์)
  2. Choppiness Index(14) — ความ "เละ" ของราคา (สูง = chop)
  3. Market Structure     — swing HH/HL = up · LH/LL = down · ปนกัน = mixed

ผลลัพธ์ 4 แบบ:
  trend_up / trend_down — เทรนด์ชัด + structure ยืนยัน → เทรดตามทิศได้
  range                 — ไม่มีเทรนด์ + chop สูง → no-trade สำหรับ trend-following
  unclear               — สัญญาณก้ำกึ่ง → ไม่บล็อก (fail-open ให้เกราะอื่นตัดสิน)

ใช้งาน:
  from regime import classify
  r = classify(df_h1)
  if r["regime"] == "range": ...skip trend-following...
"""
from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("part2.regime")


def _adx(df: pd.DataFrame, n: int = 14) -> float:
    """ADX มาตรฐาน Wilder — คืนค่าล่าสุด (0 ถ้าข้อมูลไม่พอ)"""
    if df is None or len(df) < n * 3:
        return 0.0
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values

    up = h[1:] - h[:-1]
    dn = l[:-1] - l[1:]
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))

    # Wilder smoothing (EMA alpha = 1/n)
    def _wilder(x: np.ndarray) -> np.ndarray:
        out = np.empty_like(x)
        out[0] = x[:n].mean() if len(x) >= n else x.mean()
        a = 1.0 / n
        for i in range(1, len(x)):
            out[i] = out[i - 1] + a * (x[i] - out[i - 1])
        return out

    atr_s = _wilder(tr)
    atr_s[atr_s == 0] = np.nan
    pdi = 100.0 * _wilder(plus_dm) / atr_s
    mdi = 100.0 * _wilder(minus_dm) / atr_s
    denom = pdi + mdi
    denom[denom == 0] = np.nan
    dx = 100.0 * np.abs(pdi - mdi) / denom
    dx = np.nan_to_num(dx, nan=0.0)
    adx = _wilder(dx)
    return float(adx[-1])


def _choppiness(df: pd.DataFrame, n: int = 14) -> float:
    """Choppiness Index — 100=chop สุด · 0=เทรนด์เส้นตรง
    CHOP = 100 × log10(ΣTR(n) / (maxHigh(n) − minLow(n))) / log10(n)"""
    if df is None or len(df) < n + 2:
        return 50.0
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    tr_sum = float(tr[-n:].sum())
    hh = float(h[-n:].max())
    ll = float(l[-n:].min())
    rng = hh - ll
    if rng <= 0 or tr_sum <= 0:
        return 100.0      # ราคาไม่ขยับเลย = chop เต็มขั้น
    return float(100.0 * math.log10(tr_sum / rng) / math.log10(n))


def _swings(df: pd.DataFrame, k: int = 2) -> tuple[list[float], list[float]]:
    """หา swing highs/lows แบบ fractal (สูง/ต่ำกว่าเพื่อนบ้าน k แท่งทั้งสองข้าง)
    คืน (highs, lows) เรียงตามเวลา — ตัดแท่ง forming ([-1]) ทิ้งเพราะยังไม่จบ"""
    h = df["high"].astype(float).values[:-1]
    l = df["low"].astype(float).values[:-1]
    highs, lows = [], []
    for i in range(k, len(h) - k):
        win_h = h[i - k:i + k + 1]
        win_l = l[i - k:i + k + 1]
        if h[i] == win_h.max() and (win_h == h[i]).sum() == 1:
            highs.append(h[i])
        if l[i] == win_l.min() and (win_l == l[i]).sum() == 1:
            lows.append(l[i])
    return highs, lows


def _structure(df: pd.DataFrame, k: int = 2) -> str:
    """Market Structure จาก slope ของ swing สูงสุด 4 ตัวล่าสุดต่อฝั่ง
    (ทนต่อ noise กว่าเทียบ swing คู่เดียว — swing เดียวเพี้ยนไม่พลิกผล)
    highs+lows ชันขึ้นทั้งคู่ = "up" · ลงทั้งคู่ = "down" · ขัดกัน = "mixed" """
    highs, lows = _swings(df, k)
    if len(highs) < 2 or len(lows) < 2:
        return "mixed"
    hs = np.array(highs[-4:], dtype=float)
    ls = np.array(lows[-4:], dtype=float)
    h_slope = float(np.polyfit(np.arange(len(hs)), hs, 1)[0])
    l_slope = float(np.polyfit(np.arange(len(ls)), ls, 1)[0])
    if h_slope > 0 and l_slope > 0:
        return "up"
    if h_slope < 0 and l_slope < 0:
        return "down"
    return "mixed"


def classify(df: Optional[pd.DataFrame], *,
             adx_min: float = 20.0,
             chop_max: float = 55.0,
             adx_n: int = 14,
             chop_n: int = 14) -> dict:
    """จำแนก regime จาก df (H1 แนะนำ ≥ 60 แท่ง)
    คืน {regime, adx, chop, structure, reason}"""
    if df is None or len(df) < 45:
        return {"regime": "unclear", "adx": 0.0, "chop": 50.0,
                "structure": "mixed", "reason": "ข้อมูลไม่พอ"}

    adx = _adx(df, adx_n)
    chop = _choppiness(df, chop_n)
    struct = _structure(df)

    # range = ไม่มีแรงเทรนด์ + ราคาเละ — ทั้งสองตัวชี้ต้องเห็นพ้อง (กัน false block)
    if adx < adx_min and chop > chop_max:
        return {"regime": "range", "adx": adx, "chop": chop, "structure": struct,
                "reason": f"ADX {adx:.0f} < {adx_min:.0f} + Chop {chop:.0f} > {chop_max:.0f}"}

    # เทรนด์ = แรงพอ + structure ยืนยันทิศ
    if adx >= adx_min and struct == "up":
        return {"regime": "trend_up", "adx": adx, "chop": chop, "structure": struct,
                "reason": f"ADX {adx:.0f} + HH/HL"}
    if adx >= adx_min and struct == "down":
        return {"regime": "trend_down", "adx": adx, "chop": chop, "structure": struct,
                "reason": f"ADX {adx:.0f} + LH/LL"}

    return {"regime": "unclear", "adx": adx, "chop": chop, "structure": struct,
            "reason": f"ก้ำกึ่ง (ADX {adx:.0f} · Chop {chop:.0f} · {struct})"}
