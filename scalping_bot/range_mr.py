"""
scalping_bot/range_mr.py — Range-Edge Mean Reversion (เล่นกรอบ sideways แบบมืออาชีพ)

หลักการ: ตลาด sideways ที่ "กรอบกว้างพอ" คือ edge ของ mean reversion —
ซื้อขอบล่าง/ขายขอบบน SL นอกกรอบ TP กลางกรอบ ด้วย size ปกติ
(ออกแบบมาแทนความคิด "เพิ่ม lot ในตลาดเงียบ" ซึ่งคือการขยายความเสี่ยงโดยไม่มี edge)

เกราะ 5 ชั้นก่อนยิงสัญญาณ:
  1. กรอบกว้าง ≥ min_width_atr×ATR   — เกณฑ์เรขาคณิต RR: reward ถึงกลางกรอบ ≥ 0.4×W
     ขณะ risk ≤ 0.1×W + sl_atr×ATR → RR ≥ ~2 โดยโครงสร้าง (ต้นทุนจริง spread/commission
     ตรวจ downstream ที่เกราะ "R:R หลังหักต้นทุน" ใน build_ticket — เกณฑ์นี้ไม่แทนกัน)
  2. Choppiness(ทั้งหน้าต่าง) ≥ chop_min — ยืนยันว่าทั้งกรอบคือ range จริง
     (จงใจไม่ใช้ ADX(14): ณ จังหวะราคาวิ่งมาแตะขอบ = ปลายขาเคลื่อนที่ระยะสั้น
      ADX จะสูงเสมอ → ปฏิเสธจังหวะเข้าที่ถูกต้องพอดี · Choppiness วัดทั้งหน้าต่างแทน)
  3. แตะขอบฝั่งละ ≥ min_touches ครั้ง — S/R จริง ไม่ใช่ max/min ของ drift
  4. แท่งปิดล่าสุดปิด "ในกรอบ"        — ปิดนอกกรอบ = breakout ห้ามสวน
  5. แท่ง rejection ที่ขอบ + ราคายังไม่เด้งไปไกล — ไม่ไล่ราคากลางกรอบ

ใช้งาน:
  from range_mr import signal
  sig = signal(df_m15)
  if sig["detected"]: ...  # {direction, sl, tp, width_atr, reason}
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

import regime   # ใช้ ADX ตัวเดียวกับ regime filter — เกณฑ์เดียวกันทั้งระบบ

log = logging.getLogger("part2.range_mr")


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    return float(tr[-n:].mean()) if len(tr) >= n else 0.0


def _count_touches(vals: np.ndarray, threshold: float, side: str) -> int:
    """นับจำนวน "ครั้ง" ที่ราคาแตะโซนขอบ — แท่งติดกันที่แตะต่อเนื่องนับเป็น 1 ครั้ง
    side="upper": แตะเมื่อ val >= threshold · side="lower": val <= threshold"""
    touching = vals >= threshold if side == "upper" else vals <= threshold
    touches = 0
    prev = False
    for t in touching:
        if t and not prev:
            touches += 1
        prev = t
    return touches


def signal(df: Optional[pd.DataFrame], *,
           lookback: int = 48,
           min_width_atr: float = 3.0,
           edge_pct: float = 0.10,
           min_touches: int = 2,
           sl_atr: float = 0.3,
           chop_min: float = 50.0,
           max_width_atr: float = 6.0) -> dict:
    """ตรวจ setup mean reversion ที่ขอบกรอบ — คืน {detected, direction, sl, tp, ...}
    df ควรเป็น TF สั้น (M15) ≥ lookback+60 แท่ง · ใช้เฉพาะแท่งปิดแล้ว"""
    need = max(lookback + 2, 60)
    if df is None or len(df) < need:
        return {"detected": False, "reason": "ข้อมูลไม่พอ"}

    atr = _atr(df)
    if atr <= 0:
        return {"detected": False, "reason": "ATR คำนวณไม่ได้"}

    # ตัดแท่ง forming ทิ้ง — ทุกการตัดสินใจใช้แท่งปิดแล้วเท่านั้น
    h = df["high"].astype(float).values[-(lookback + 1):-1]
    l = df["low"].astype(float).values[-(lookback + 1):-1]
    bar = df.iloc[-2]                                   # แท่งปิดล่าสุด
    b_open, b_close = float(bar["open"]), float(bar["close"])
    b_high, b_low = float(bar["high"]), float(bar["low"])

    range_high = float(h.max())
    range_low = float(l.min())
    width = range_high - range_low

    # ── 1. กรอบต้องกว้างพอหลังหักต้นทุน — แต่ไม่กว้างเกินจนไม่ใช่กรอบ ──
    width_atr = width / atr
    if width_atr < min_width_atr:
        return {"detected": False,
                "reason": f"กรอบแคบ {width_atr:.1f}×ATR < {min_width_atr:.1f}"}
    # กรอบกว้างเกิน = มักเป็น "เทรนด์แรง/ขาลงดิ่ง + เด้ง" ที่ถูกอ่านผิดเป็นกรอบ
    # (เคสจริง: ETH กรอบ 9.9×ATR → ซื้อขอบล่าง = รับมีดตก แดงทันที)
    if max_width_atr > 0 and width_atr > max_width_atr:
        return {"detected": False,
                "reason": f"กรอบกว้างเกิน {width_atr:.1f}×ATR > {max_width_atr:.1f} — น่าจะเทรนด์ ไม่ใช่ sideways"}

    # ── 2. ทั้งหน้าต่างต้องเป็น range จริง (Choppiness สูง = ราคาวนในกรอบ) ──
    chop = regime._choppiness(df, n=lookback)
    if chop < chop_min:
        return {"detected": False, "reason": f"Chop {chop:.0f} < {chop_min:.0f} — มีเทรนด์ ไม่ใช่กรอบ"}

    # ── 3. ขอบต้องถูกแตะซ้ำ — เป็น S/R จริง ไม่ใช่ปลายของ drift ─────────
    zone = width * edge_pct
    up_touch = _count_touches(h, range_high - zone, "upper")
    lo_touch = _count_touches(l, range_low + zone, "lower")
    if up_touch < min_touches or lo_touch < min_touches:
        return {"detected": False,
                "reason": f"แตะขอบไม่พอ (บน {up_touch} · ล่าง {lo_touch} < {min_touches})"}

    # ── 4. แท่งล่าสุดต้องปิดในกรอบ — ปิดนอก = breakout ห้ามสวน ─────────
    if not (range_low < b_close < range_high):
        return {"detected": False, "reason": "แท่งปิดนอกกรอบ — breakout"}

    mid = (range_high + range_low) / 2.0
    close_zone = 1.5 * zone        # ราคาปิดต้องยังใกล้ขอบ — ปิดไกลกว่านี้ = ไล่ราคา RR เสีย

    def _result(direction: str, sl: float, entry_ref: float, touches: int, edge_name: str) -> dict:
        # RR precheck จาก close ของแท่ง rejection (ก่อนต้นทุน — เกราะต้นทุนจริงอยู่ downstream)
        rr_est = (mid - entry_ref) / max(entry_ref - sl, 1e-9) if direction == "buy" \
            else (entry_ref - mid) / max(sl - entry_ref, 1e-9)
        if rr_est < 1.3:
            return {"detected": False,
                    "reason": f"RR เรขาคณิต {rr_est:.2f} < 1.3 (เข้าไกลขอบไป)"}
        return {
            "detected": True, "direction": direction,
            "sl": sl, "tp": mid,
            "range_high": range_high, "range_low": range_low,
            "width_atr": width_atr, "chop": chop,
            "reason": f"rejection ขอบ{edge_name} (กรอบ {width_atr:.1f}×ATR · แตะ {touches} ครั้ง · RR~{rr_est:.1f})",
        }

    # BUY:  wick แตะโซนขอบล่าง · ปิดเขียว (rejection) · close ยังอยู่ติดขอบ
    if b_low <= range_low + zone and b_close > b_open and b_close <= range_low + close_zone:
        return _result("buy", range_low - sl_atr * atr, b_close, lo_touch, "ล่าง")
    # SELL: wick แตะโซนขอบบน · ปิดแดง · close ยังอยู่ติดขอบ
    if b_high >= range_high - zone and b_close < b_open and b_close >= range_high - close_zone:
        return _result("sell", range_high + sl_atr * atr, b_close, up_touch, "บน")

    return {"detected": False, "reason": "ราคาไม่อยู่ขอบกรอบ/ไม่มี rejection"}
