"""
part2_mt5/scalp.py — กลยุทธ์เทรดสั้น "ตามเทรนด์" ระหว่างรอสัญญาณหลัก (CDC)
เปิดใช้งานผ่าน flag เท่านั้น (USE_EMA_STOCH / USE_ORB) — ดีฟอลต์ปิด จนกว่าจะ backtest ผ่าน

หลักคิด: ของพวกนี้ "เติมจังหวะระหว่างวัน" ไม่ใช่หัวใจ — ต้องผ่านเกราะเดิมทุกด่าน
(RSI สุดขั้ว, MIN_RR, spread, MAX_RISK, Gemini, กระจายกลุ่ม) เหมือนไม้ปกติ

#1 EMA Ribbon + Stochastic = ตามเทรนด์ (price>EMA50>EMA200) + ย่อแตะ EMA50 + Stoch ตัดขึ้นจาก oversold
#3 Opening Range Breakout (ORB) = เบรกกรอบ High/Low ช่วงตลาดเปิด (ผันผวนสูง)

ทำงานบน DataFrame OHLC (คอลัมน์ time/open/high/low/close/volume) — ไม่พึ่ง MT5/Part 1
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.astype(float).ewm(span=n, adjust=False).mean()


def _atr(df, n: int = 20) -> float:
    """ATR แบบเรียบ (mean range) — ใช้เป็นกันชน SL/tolerance"""
    rng = (df["high"].astype(float) - df["low"].astype(float))
    return float(rng.iloc[-n:].mean()) if len(rng) >= 5 else float(rng.mean())


def stochastic(df, k_period: int = 14, k_smooth: int = 3, d_period: int = 3):
    """Stochastic Oscillator (14,3,3) → คืน (%K, %D) เป็น pandas Series
    %K = ราคาปิดอยู่ตรงไหนของกรอบ high-low ล่าสุด · %D = เส้นเฉลี่ยของ %K"""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    ll = low.rolling(k_period).min()
    hh = high.rolling(k_period).max()
    rng = (hh - ll).replace(0, np.nan)
    raw_k = 100.0 * (close - ll) / rng
    k = raw_k.rolling(k_smooth).mean()
    d = k.rolling(d_period).mean()
    return k.fillna(50.0), d.fillna(50.0)


def ema_ribbon_stoch(df, atr: Optional[float] = None,
                     oversold: float = 20.0, overbought: float = 80.0) -> dict:
    """#1 EMA Ribbon + Stochastic (ตามเทรนด์ ห้ามสวน)
      Buy : close > EMA50 > EMA200 (เทรนด์ขึ้นชัด) + เพิ่งย่อแตะ EMA50 + Stoch ตัดขึ้นจาก oversold
      Sell: close < EMA50 < EMA200 + เพิ่งเด้งแตะ EMA50 + Stoch ตัดลงจาก overbought
    คืน {detected, direction, entry, sl, reason} · SL = ใต้/เหนือ EMA50/แกว่งล่าสุด"""
    if df is None or len(df) < 210:        # ต้องพอคำนวณ EMA200 + Stoch
        return {"detected": False}
    close = df["close"].astype(float)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200)
    c = float(close.iloc[-1])
    e50 = float(ema50.iloc[-1])
    e200 = float(ema200.iloc[-1])
    atr = atr if (atr and atr > 0) else _atr(df)
    if atr <= 0:
        return {"detected": False}

    k, d = stochastic(df)
    k1, k0, k_1 = float(k.iloc[-1]), float(k.iloc[-2]), float(k.iloc[-3])
    d1, d0 = float(d.iloc[-1]), float(d.iloc[-2])

    # ขาขึ้น
    if c > e50 > e200:
        recent_low = float(df["low"].iloc[-5:].min())
        near = recent_low <= e50 + atr * 0.5                 # เพิ่งย่อมาแตะโซน EMA50
        from_oversold = min(k_1, k0) <= oversold             # %K เคยลงไปแดนขายมากเกิน
        cross_up = (k0 <= d0) and (k1 > d1)                  # แล้วตัด %D ขึ้น
        if near and from_oversold and cross_up:
            sl = round(min(recent_low, e50) - atr * 0.3, 5)
            if sl < c:
                return {"detected": True, "direction": "buy", "entry": round(c, 5), "sl": sl,
                        "reason": "EMA50>EMA200 + ย่อแตะ EMA50 + Stoch ตัดขึ้นจาก oversold"}
    # ขาลง (มิเรอร์)
    if c < e50 < e200:
        recent_high = float(df["high"].iloc[-5:].max())
        near = recent_high >= e50 - atr * 0.5
        from_overbought = max(k_1, k0) >= overbought
        cross_dn = (k0 >= d0) and (k1 < d1)
        if near and from_overbought and cross_dn:
            sl = round(max(recent_high, e50) + atr * 0.3, 5)
            if sl > c:
                return {"detected": True, "direction": "sell", "entry": round(c, 5), "sl": sl,
                        "reason": "EMA50<EMA200 + เด้งแตะ EMA50 + Stoch ตัดลงจาก overbought"}
    return {"detected": False}


def opening_range_breakout(df, open_hours=(14, 15, 20), range_min: int = 15,
                           valid_min: int = 180, atr: Optional[float] = None) -> dict:
    """#3 Opening Range Breakout: กรอบ High/Low ช่วง 'range_min' นาทีแรกหลังตลาดเปิด
      เบรกกรอบบน = buy · เบรกกรอบล่าง = sell · SL = กึ่งกลางกรอบ (R:R ~2) · TP = ความกว้างกรอบ
    open_hours = ชั่วโมงตลาดเปิดสำคัญ (เวลา 'ของแท่ง MT5' = server time) — ปรับให้ตรงโบรกได้
    valid_min = เข้าได้ภายในกี่นาทีหลังเปิด (เลยช่วงนี้ = หมดความผันผวนต้น session)"""
    if df is None or len(df) < 5 or "time" not in df.columns:
        return {"detected": False}
    t = pd.to_datetime(df["time"])
    last_t = t.iloc[-1]

    # หา session-open ล่าสุดที่ <= แท่งปัจจุบัน
    best_open = None
    for h in open_hours:
        cand = last_t.normalize() + pd.Timedelta(hours=int(h))
        if cand > last_t:
            cand -= pd.Timedelta(days=1)
        if best_open is None or cand > best_open:
            best_open = cand
    if best_open is None:
        return {"detected": False}

    mins_since = (last_t - best_open).total_seconds() / 60.0
    if mins_since < range_min or mins_since > valid_min:     # ยังไม่ครบกรอบ / เลยหน้าต่างเข้าแล้ว
        return {"detected": False}

    mask = (t >= best_open) & (t < best_open + pd.Timedelta(minutes=range_min))
    rbars = df[mask.values]
    if len(rbars) < 1:
        return {"detected": False}
    or_hi = float(rbars["high"].max())
    or_lo = float(rbars["low"].min())
    width = or_hi - or_lo
    if width <= 0:
        return {"detected": False}

    c = float(df["close"].iloc[-1])
    atr = atr if (atr and atr > 0) else _atr(df)
    mid = or_lo + width / 2.0
    hhmm = best_open.strftime("%H:%M")
    if c > or_hi:                                            # เบรกขึ้น
        return {"detected": True, "direction": "buy", "entry": round(c, 5),
                "sl": round(mid, 5), "tp": round(c + width, 5),
                "reason": f"ORB เบรกกรอบเปิด {hhmm} ขึ้น (กว้าง {round(width, 5)})"}
    if c < or_lo:                                            # เบรกลง
        return {"detected": True, "direction": "sell", "entry": round(c, 5),
                "sl": round(mid, 5), "tp": round(c - width, 5),
                "reason": f"ORB เบรกกรอบเปิด {hhmm} ลง (กว้าง {round(width, 5)})"}
    return {"detected": False}


def asian_london_orb(df, asian_start: int = 0, asian_end: int = 6,
                     london_start: int = 7, london_end: int = 11) -> dict:
    """#3-FX Asian-Range → London Breakout (กลยุทธ์ที่ backtest ผ่านบน FX):
      ตีกรอบ High/Low ช่วงเอเชีย (00:00-06:00) → เบรกตอน London เปิด (07:00-11:00) ตามน้ำ
      SL = กึ่งกลางกรอบ (R:R ~2) · TP = ความกว้างกรอบ · เข้าเฉพาะ 'แท่งแรก' ที่เบรก
    *** เวลาอิง server (โบรกนี้ = UTC+0) — ปรับ asian/london hours ได้ถ้า DST/โบรกต่าง ***"""
    if df is None or len(df) < 30 or "time" not in df.columns:
        return {"detected": False}
    t = pd.to_datetime(df["time"])
    last = t.iloc[-1]
    if not (london_start <= last.hour < london_end):        # นอกหน้าต่าง London → ไม่เข้า
        return {"detected": False}
    day0 = last.normalize()
    mask = (t >= day0 + pd.Timedelta(hours=asian_start)) & (t < day0 + pd.Timedelta(hours=asian_end))
    ab = df[mask.values]
    if len(ab) < 4:
        return {"detected": False}
    ah, al = float(ab["high"].max()), float(ab["low"].min())
    width = ah - al
    if width <= 0:
        return {"detected": False}
    c, pc = float(df["close"].iloc[-1]), float(df["close"].iloc[-2])
    mid = al + width / 2.0
    if c > ah and pc <= ah:                                 # แท่งแรกที่เบรกกรอบเอเชียขึ้น
        return {"detected": True, "direction": "buy", "entry": round(c, 5),
                "sl": round(mid, 5), "tp": round(c + width, 5),
                "reason": f"ORB เบรกกรอบเอเชียขึ้น (London) กว้าง {round(width, 5)}"}
    if c < al and pc >= al:
        return {"detected": True, "direction": "sell", "entry": round(c, 5),
                "sl": round(mid, 5), "tp": round(c - width, 5),
                "reason": f"ORB เบรกกรอบเอเชียลง (London) กว้าง {round(width, 5)}"}
    return {"detected": False}


def _rsi(s, n: int = 14):
    d = s.astype(float).diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean().replace(0, np.nan)
    return (100 - 100 / (1 + up / dn)).fillna(50)


def _bull_trigger(df) -> bool:
    """แท่งกลับตัวขาขึ้น: Bullish Engulfing หรือ Pin Bar (หางล่างยาว)"""
    o, c = float(df["open"].iloc[-1]), float(df["close"].iloc[-1])
    po, pc = float(df["open"].iloc[-2]), float(df["close"].iloc[-2])
    h, l = float(df["high"].iloc[-1]), float(df["low"].iloc[-1])
    rng = h - l
    engulf = c > o and pc < po and c >= po and o <= pc
    pin = rng > 0 and (min(o, c) - l) > 0.6 * rng and c > o
    return bool(engulf or pin)


def _bear_trigger(df) -> bool:
    o, c = float(df["open"].iloc[-1]), float(df["close"].iloc[-1])
    po, pc = float(df["open"].iloc[-2]), float(df["close"].iloc[-2])
    h, l = float(df["high"].iloc[-1]), float(df["low"].iloc[-1])
    rng = h - l
    engulf = c < o and pc > po and c <= po and o >= pc
    pin = rng > 0 and (h - max(o, c)) > 0.6 * rng and c < o
    return bool(engulf or pin)


def hybrid_pro(df, rr: float = 2.5, rsi_lo: float = 40, rsi_hi: float = 60) -> dict:
    """Hybrid-Pro (multi-TF ตามเทรนด์): H1 EMA50>EMA200 = เทรนด์ + M15 ย่อแตะ EMA20 +
      RSI 40-60 (โซนพักตัว) + แท่งกลับตัว (Engulfing/Pin) → เข้าตามเทรนด์
      SL = swing low/high ล่าสุด · TP = rr × ระยะ SL (ดีฟอลต์ 2.5)
    *** ใช้ M15 df (≥850 แท่ง) — resample เป็น H1 ในตัวเพื่อหาเทรนด์ใหญ่ ***
    backtest: BTC +8.4 · ETH +8.2 · ทอง +3.5 · USTEC +1.2 (หลังหัก spread)"""
    if df is None or len(df) < 850 or "time" not in df.columns:
        return {"detected": False}
    close = df["close"].astype(float)
    e20 = float(_ema(close, 20).iloc[-1])
    rsi = float(_rsi(close, 14).iloc[-1])
    atr = _atr(df)
    if atr <= 0:
        return {"detected": False}
    c = float(close.iloc[-1])
    lo, hi = float(df["low"].iloc[-1]), float(df["high"].iloc[-1])
    # เทรนด์ใหญ่จาก H1 (resample M15 → H1)
    h = df.set_index(pd.to_datetime(df["time"])).resample("1h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if len(h) < 210:
        return {"detected": False}
    he50 = float(h["close"].ewm(span=50, adjust=False).mean().iloc[-1])
    he200 = float(h["close"].ewm(span=200, adjust=False).mean().iloc[-1])
    hc = float(h["close"].iloc[-1])
    up = he50 > he200 and hc > he50
    dn = he50 < he200 and hc < he50
    if up and lo <= e20 + 0.3 * atr and rsi_lo <= rsi <= rsi_hi and _bull_trigger(df):
        sl = round(float(df["low"].iloc[-6:].min()) - 0.2 * atr, 5)
        if sl < c:
            return {"detected": True, "direction": "buy", "entry": round(c, 5),
                    "sl": sl, "tp": round(c + rr * (c - sl), 5),
                    "reason": "Hybrid-Pro: H1 ขาขึ้น + ย่อ EMA20 + RSI 40-60 + แท่งกลับตัว"}
    if dn and hi >= e20 - 0.3 * atr and rsi_lo <= rsi <= rsi_hi and _bear_trigger(df):
        sl = round(float(df["high"].iloc[-6:].max()) + 0.2 * atr, 5)
        if sl > c:
            return {"detected": True, "direction": "sell", "entry": round(c, 5),
                    "sl": sl, "tp": round(c - rr * (sl - c), 5),
                    "reason": "Hybrid-Pro: H1 ขาลง + เด้ง EMA20 + RSI 40-60 + แท่งกลับตัว"}
    return {"detected": False}
