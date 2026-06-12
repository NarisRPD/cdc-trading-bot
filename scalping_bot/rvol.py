"""
scalping_bot/rvol.py — RVOL Spurt + Breakout ("In-Play" momentum scalping)

หลักการ: volume ที่พุ่งผิดปกติ = มี catalyst (ข่าว/งบ/flow ใหญ่) — สินทรัพย์กลายเป็น
"In-Play" ชั่วคราว ช่วงนั้น breakout ของระดับสำคัญมี follow-through สูงกว่าปกติ

RVOL (Relative Volume) ≠ volume เทียบแท่งข้างเคียง (แบบที่ bb_squeeze ใช้):
เทียบกับ "ค่าเฉลี่ยของเวลาเดียวกันในวันก่อนๆ" — เพราะ volume มี seasonality รายวันสูง
(เปิด London/NY volume สูงเสมอ ถ้าเทียบแท่งข้างเคียงจะ false positive ทุกเช้า)

เกราะก่อนยิงสัญญาณ:
  1. RVOL ≥ rvol_min          — volume ชั่วโมงนี้ ≥ N เท่าของเวลาเดียวกันย้อนหลัง
  2. ปิดทะลุ high/low N แท่ง   — breakout ของระดับที่มีความหมาย (Donchian)
  3. แท่งก่อนหน้ายังไม่ทะลุ     — กัน late entry (เข้าเฉพาะแท่งเบรคแรก)
  4. body แท่งเบรค ≥ body_min  — เบรคด้วยแรงจริง ไม่ใช่ wick แหย่
SL ใต้แท่งเบรค (ตามตำรา momentum scalp: เบรคแท้ต้องไม่ย้อนกลับเข้ากรอบ)
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("part2.rvol")


def _vol_col(df: pd.DataFrame) -> Optional[str]:
    return ("tick_volume" if "tick_volume" in df.columns
            else "volume" if "volume" in df.columns else None)


def rvol(df: pd.DataFrame, window_bars: int = 4, baseline_days: int = 5) -> Optional[float]:
    """RVOL ของ window_bars แท่งปิดล่าสุด เทียบค่าเฉลี่ย "เวลาเดียวกัน" ย้อนหลัง N วัน
    คืน None ถ้าประวัติไม่พอ (ไม่พอ = อ้าง in-play ไม่ได้ → ผู้เรียกควรข้าม)"""
    vc = _vol_col(df)
    if vc is None or len(df) < window_bars + 2:
        return None

    d = df.copy()
    if not pd.api.types.is_datetime64_any_dtype(d["time"]):
        d["utc"] = pd.to_datetime(d["time"], unit="s", utc=True)
    else:
        d["utc"] = d["time"]
    d["clock"] = d["utc"].dt.strftime("%H:%M")

    closed = d.iloc[:-1]                      # ตัดแท่ง forming
    cur = closed.iloc[-window_bars:]          # หน้าต่างปัจจุบัน
    hist = closed.iloc[:-window_bars]         # ประวัติก่อนหน้า

    baselines = []
    for _, row in cur.iterrows():
        same_clock = hist[hist["clock"] == row["clock"]][vc].tail(baseline_days)
        if len(same_clock) >= max(2, baseline_days - 2):   # ยอมขาดได้ 2 วัน (วันหยุด)
            baselines.append(float(same_clock.mean()))
    if len(baselines) < window_bars:
        return None                           # ประวัติไม่พอ — ห้ามเดา

    base = float(np.mean(baselines))
    if base <= 0:
        return None
    return float(cur[vc].mean()) / base


def signal(df: Optional[pd.DataFrame], *,
           window_bars: int = 4,
           baseline_days: int = 5,
           rvol_min: float = 2.0,
           break_bars: int = 32,
           body_min: float = 0.5,
           sl_atr_buf: float = 0.2) -> dict:
    """ตรวจ RVOL spurt + breakout — คืน {detected, direction, sl, rvol, reason}"""
    if df is None or len(df) < break_bars + window_bars + 5:
        return {"detected": False, "reason": "ข้อมูลไม่พอ"}

    rv = rvol(df, window_bars, baseline_days)
    if rv is None:
        return {"detected": False, "reason": "ประวัติ volume ไม่พอคำนวณ RVOL"}
    if rv < rvol_min:
        return {"detected": False, "reason": f"RVOL {rv:.1f} < {rvol_min:.1f} — ไม่ in-play"}

    closed = df.iloc[:-1]                     # แท่งปิดแล้วเท่านั้น
    bar = closed.iloc[-1]                     # แท่งเบรค (ปิดล่าสุด)
    prev = closed.iloc[-2]
    ref = closed.iloc[-(break_bars + 1):-1]   # ระดับอ้างอิง: N แท่งก่อนแท่งเบรค

    hi = float(ref["high"].max())
    lo = float(ref["low"].min())
    b_open, b_close = float(bar["open"]), float(bar["close"])
    b_high, b_low = float(bar["high"]), float(bar["low"])

    h = closed["high"].astype(float).values
    l = closed["low"].astype(float).values
    c = closed["close"].astype(float).values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr = float(tr[-14:].mean()) if len(tr) >= 14 else 0.0
    if atr <= 0:
        return {"detected": False, "reason": "ATR คำนวณไม่ได้"}

    rng = b_high - b_low
    body = abs(b_close - b_open) / rng if rng > 0 else 0.0

    # BUY: ปิดทะลุ high — แท่งก่อนยังไม่ทะลุ (เบรคแรกเท่านั้น) + body แน่น
    if b_close > hi and float(prev["close"]) <= hi:
        if body < body_min:
            return {"detected": False, "reason": f"body {body:.0%} < {body_min:.0%} — เบรคไม่มีแรง"}
        return {"detected": True, "direction": "buy", "rvol": rv,
                "sl": b_low - sl_atr_buf * atr,
                "reason": f"RVOL {rv:.1f}× + เบรค high {break_bars} แท่ง (body {body:.0%})"}
    # SELL: ปิดทะลุ low
    if b_close < lo and float(prev["close"]) >= lo:
        if body < body_min:
            return {"detected": False, "reason": f"body {body:.0%} < {body_min:.0%} — เบรคไม่มีแรง"}
        return {"detected": True, "direction": "sell", "rvol": rv,
                "sl": b_high + sl_atr_buf * atr,
                "reason": f"RVOL {rv:.1f}× + เบรค low {break_bars} แท่ง (body {body:.0%})"}

    return {"detected": False, "reason": f"RVOL {rv:.1f}× แต่ยังไม่เบรคระดับ {break_bars} แท่ง"}
