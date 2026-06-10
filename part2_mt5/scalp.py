"""
part2_mt5/scalp.py — กลยุทธ์เทรดสั้น (ไม่พึ่ง CDC — ทำงานจาก OHLC ล้วน)

กลยุทธ์ทั้งหมดต้องผ่านเกราะเดิมทุกด่าน:
(RSI สุดขั้ว, MIN_RR, spread, MAX_RISK, Gemini, กระจายกลุ่ม)

#1 SuperTrend (ATR-based) = เทรนด์ผ่อน-ผัน ปรับตามความผันผวนจริง ไม่ lagging เท่า EMA crossover
#2 EMA Ribbon + Stochastic = ตามเทรนด์ (price>EMA50>EMA200) + ย่อแตะ EMA50 + Stoch ตัดขึ้นจาก oversold
#3 Opening Range Breakout (ORB) = เบรกกรอบ High/Low ช่วงตลาดเปิด (ผันผวนสูง)
#4 Hybrid-Pro = H1 EMA50>EMA200 + M15 ย่อ EMA20 + RSI 40-60 + แท่งกลับตัว

ทำงานบน DataFrame OHLC (คอลัมน์ time/open/high/low/close/volume) — ไม่พึ่ง MT5/Part 1
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.astype(float).ewm(span=n, adjust=False).mean()


def _atr(df, n: int = 20) -> float:
    """ATR แบบ True Range (รวม gap ข้ามแท่ง) — แม่นกว่า high-low อย่างเดียว โดยเฉพาะ crypto
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)"""
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    if len(c) < 2:
        return float(np.mean(h - l)) if len(h) >= 1 else 0.0
    pc = np.roll(c, 1)
    pc[0] = c[0]           # แท่งแรก: prev_close = close ตัวเอง (ไม่มี gap ก่อนหน้า)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return float(tr[-n:].mean()) if len(tr) >= 5 else float(tr.mean())


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
                     oversold: float = 20.0, overbought: float = 80.0,
                     sl_atr_mult: float = 0.6) -> dict:
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
            sl = round(min(recent_low, e50) - atr * sl_atr_mult, 5)
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
            sl = round(max(recent_high, e50) + atr * sl_atr_mult, 5)
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


def supertrend(df, period: int = 10, mult: float = 3.0) -> dict:
    """#1 SuperTrend indicator แบบ numpy — ATR-based ปรับตามความผันผวนจริง
    ดีกว่า CDC EMA crossover สำหรับเทรดสั้น: ไม่ lagging, ลด whipsaw ในตลาดผันผวน

    คืน dict: {st, direction (+1=buy/-1=sell), flipped, upper, lower}
    band จะขยับทิศเดียว (ratchet) → กันสัญญาณหลอกซ้ำๆ ในกรอบแคบ"""
    n = len(df)
    if n < period + 5:
        return {"st": None}
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values

    # ATR (True Range) — full series
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr_arr = np.zeros(n)
    for i in range(period - 1, n):
        atr_arr[i] = tr[max(0, i - period + 1):i + 1].mean()
    atr_arr[:period - 1] = atr_arr[period - 1]

    hl2 = (h + l) / 2.0
    bu = hl2 + mult * atr_arr   # basic upper band
    bl = hl2 - mult * atr_arr   # basic lower band

    # Final bands — band ขยับได้ทิศเดียว (ratchet effect → กัน whipsaw)
    fu, fl = bu.copy(), bl.copy()
    for i in range(1, n):
        fu[i] = bu[i] if (bu[i] < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = bl[i] if (bl[i] > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]

    # SuperTrend: ติดตาม lower band ตอนขาขึ้น, upper band ตอนขาลง
    st = fu.copy()
    dr = np.full(n, -1, dtype=int)   # -1=sell, +1=buy
    for i in range(1, n):
        if st[i-1] == fu[i-1]:       # ก่อนหน้าเป็นขาลง
            st[i] = fu[i] if c[i] <= fu[i] else fl[i]
        else:                          # ก่อนหน้าเป็นขาขึ้น
            st[i] = fl[i] if c[i] >= fl[i] else fu[i]
        dr[i] = 1 if st[i] == fl[i] else -1

    flipped = np.zeros(n, dtype=bool)
    flipped[1:] = dr[1:] != dr[:-1]   # แท่งที่เพิ่งเปลี่ยนทิศ

    return {"st": st, "direction": dr, "flipped": flipped, "upper": fu, "lower": fl}


def supertrend_signal(df, period: int = 10, mult: float = 3.0,
                      fresh_bars: int = 3, sl_atr_mult: float = 0.3) -> dict:
    """สัญญาณ SuperTrend — ตรวจ flip ล่าสุด แล้วส่งคืน signal dict
    fresh_bars: นับว่าใหม่ถ้า flip เกิดใน N แท่งล่าสุด (ไม่เอาสัญญาณเก่าค้าง)
    SL = SuperTrend line ± sl_atr_mult * ATR (กันชนไม่ให้โดน noise เขี่ย)
    คืน {detected, direction, entry, sl, st_value, flip_bars_ago, reason}"""
    if df is None or len(df) < period + 20:
        return {"detected": False}
    result = supertrend(df, period, mult)
    if result.get("st") is None:
        return {"detected": False}

    st_arr = result["st"]
    dr_arr = result["direction"]
    flip_arr = result["flipped"]

    # หา flip ล่าสุดใน fresh_bars แท่ง
    flip_bar = 0
    for i in range(1, min(fresh_bars + 1, len(flip_arr) + 1)):
        if flip_arr[-i]:
            flip_bar = i
            break
    if not flip_bar:
        return {"detected": False}

    direction = "buy" if int(dr_arr[-1]) == 1 else "sell"
    c = float(df["close"].iloc[-1])
    st_val = float(st_arr[-1])
    atr_val = _atr(df)

    # SL อิง SuperTrend line (แนวรับ/ต้าน dynamic) + ATR buffer เล็กน้อยกัน noise
    if direction == "buy":
        sl = round(st_val - sl_atr_mult * atr_val, 5)
        if sl >= c:   # SL ผิดด้าน (ราคาเลยไปแล้ว) → ข้าม
            return {"detected": False}
    else:
        sl = round(st_val + sl_atr_mult * atr_val, 5)
        if sl <= c:
            return {"detected": False}

    return {
        "detected": True,
        "direction": direction,
        "entry": round(c, 5),
        "sl": sl,
        "st_value": round(st_val, 5),
        "flip_bars_ago": flip_bar,
        "reason": f"SuperTrend flip {flip_bar} แท่งที่แล้ว → {direction.upper()} (ST={round(st_val, 4)})"
    }


# ─────────────────────────────────────────────────────────────────────────────
# #2  HalfTrend — smooth ATR-based trend, ลด whipsaw ดีกว่า SuperTrend
#     ในตลาดผันผวนช่วงสั้น เพราะติดตาม swing high/low แบบ ratchet + EMA smooth
# ─────────────────────────────────────────────────────────────────────────────

def halftrend(df, amplitude: int = 2, channel_dev: float = 2.0) -> dict:
    """HalfTrend indicator — ติดตาม swing high/low ด้วย EMA smooth
    amplitude: ATR period สำหรับ dev band (default 2)
    channel_dev: ATR multiplier ของ channel (default 2.0)
    คืน dict: {ht, direction (+1=buy/-1=sell), flipped}"""
    n = len(df)
    if n < max(amplitude * 3, 20):
        return {"ht": None}
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    hl2 = (h + l) / 2.0

    # ATR (True Range series)
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr_arr = np.zeros(n)
    ap = max(amplitude, 1)
    for i in range(ap - 1, n):
        atr_arr[i] = tr[max(0, i - ap + 1):i + 1].mean()
    atr_arr[:ap - 1] = atr_arr[ap - 1] if n > ap else 0.001

    # EMA smooth ของ high/low (span=2 → alpha=2/3 ตอบสนองเร็วแต่ไม่ noisy)
    alpha = 2.0 / 3.0
    eh, el = h.copy().astype(float), l.copy().astype(float)
    for i in range(1, n):
        eh[i] = alpha * h[i] + (1 - alpha) * eh[i - 1]
        el[i] = alpha * l[i] + (1 - alpha) * el[i - 1]

    # state machine: ติดตาม max_low (uptrend) / min_high (downtrend)
    trend = np.zeros(n, dtype=int)         # 0=up, 1=down
    nxt   = np.zeros(n, dtype=int)         # nextTrend
    max_lo = hl2.copy()
    min_hi = hl2.copy()
    ht = hl2.copy()
    dr = np.ones(n, dtype=int)             # +1=buy, -1=sell

    for i in range(1, n):
        if nxt[i - 1] == 1:               # กำลังมองหา downtrend
            max_lo[i] = max(l[i], max_lo[i - 1])
            if eh[i] < max_lo[i] and c[i] < l[i - 1]:
                trend[i] = 1;  nxt[i] = 0;  min_hi[i] = h[i]
            else:
                trend[i] = trend[i - 1];  nxt[i] = nxt[i - 1]
                min_hi[i] = min_hi[i - 1]
        else:                              # กำลังมองหา uptrend
            min_hi[i] = min(h[i], min_hi[i - 1])
            if el[i] > min_hi[i] and c[i] > h[i - 1]:
                trend[i] = 0;  nxt[i] = 1;  max_lo[i] = l[i]
            else:
                trend[i] = trend[i - 1];  nxt[i] = nxt[i - 1]
                max_lo[i] = max_lo[i - 1]

        if trend[i] == 0:                  # uptrend: ht ขยับขึ้นตาม max_low
            ht[i] = max(ht[i - 1], max_lo[i])
            dr[i] = 1
        else:                              # downtrend: ht ขยับลงตาม min_high
            ht[i] = min(ht[i - 1], min_hi[i])
            dr[i] = -1

    flipped = np.zeros(n, dtype=bool)
    flipped[1:] = dr[1:] != dr[:-1]
    upper_ch = ht - atr_arr * channel_dev  # channel ด้านล่าง (แนวรับ)
    lower_ch = ht + atr_arr * channel_dev  # channel ด้านบน (แนวต้าน)
    return {"ht": ht, "direction": dr, "flipped": flipped,
            "upper_ch": upper_ch, "lower_ch": lower_ch}


def halftrend_signal(df, amplitude: int = 2, channel_dev: float = 2.0,
                     fresh_bars: int = 3, sl_atr_mult: float = 0.3) -> dict:
    """สัญญาณ HalfTrend — ตรวจ flip ล่าสุด, SL อิง HalfTrend line + ATR buffer"""
    if df is None or len(df) < max(amplitude * 3, 20) + 5:
        return {"detected": False}
    res = halftrend(df, amplitude, channel_dev)
    if res.get("ht") is None:
        return {"detected": False}

    dr_arr, flip_arr, ht_arr = res["direction"], res["flipped"], res["ht"]
    flip_bar = 0
    for i in range(1, min(fresh_bars + 1, len(flip_arr) + 1)):
        if flip_arr[-i]:
            flip_bar = i; break
    if not flip_bar:
        return {"detected": False}

    direction = "buy" if int(dr_arr[-1]) == 1 else "sell"
    c = float(df["close"].iloc[-1])
    ht_val = float(ht_arr[-1])
    atr_val = _atr(df)

    if direction == "buy":
        sl = round(ht_val - sl_atr_mult * atr_val, 5)
        if sl >= c: return {"detected": False}
    else:
        sl = round(ht_val + sl_atr_mult * atr_val, 5)
        if sl <= c: return {"detected": False}

    return {"detected": True, "direction": direction, "entry": round(c, 5),
            "sl": sl, "ht_value": round(ht_val, 5), "flip_bars_ago": flip_bar,
            "reason": f"HalfTrend flip {flip_bar} แท่งที่แล้ว → {direction.upper()} (HT={round(ht_val, 4)})"}


# ─────────────────────────────────────────────────────────────────────────────
# #3  UT Bot Alerts — ATR Trailing Stop + EMA crossover
#     ยอดนิยม TradingView: ตอบสนองไว, Buy/Sell signal ชัดเจน ไม่ต้องตีความ
# ─────────────────────────────────────────────────────────────────────────────

def utbot(df, key_value: float = 1.0, atr_period: int = 10) -> dict:
    """UT Bot Alerts indicator — ATR trailing stop แบบ adaptive
    key_value: sensitivity (ต่ำ=ไว,สัญญาณเยอะ · สูง=ช้า,สัญญาณน้อย) ค่าแนะนำ 1-2
    คืน dict: {ts (trailing stop), direction (+1=buy/-1=sell), flipped}"""
    n = len(df)
    if n < atr_period + 5:
        return {"ts": None}
    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values

    # ATR
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    atr_arr = np.zeros(n)
    for i in range(atr_period - 1, n):
        atr_arr[i] = tr[max(0, i - atr_period + 1):i + 1].mean()
    atr_arr[:atr_period - 1] = atr_arr[atr_period - 1]

    nATR = atr_arr * key_value     # ATR ปรับด้วย key_value (sensitivity)

    # Trailing Stop — ratchet ตาม direction (ขยับทิศเดียว)
    ts = c.copy()
    for i in range(1, n):
        prev = ts[i - 1]
        if c[i] > prev and c[i - 1] > prev:
            ts[i] = max(prev, c[i] - nATR[i])    # ขาขึ้น: TS ขยับขึ้นเท่านั้น
        elif c[i] < prev and c[i - 1] < prev:
            ts[i] = min(prev, c[i] + nATR[i])    # ขาลง: TS ขยับลงเท่านั้น
        elif c[i] > prev:
            ts[i] = c[i] - nATR[i]               # ราคาเพิ่งข้ามขึ้น → reset TS
        else:
            ts[i] = c[i] + nATR[i]               # ราคาเพิ่งข้ามลง → reset TS

    dr = np.where(c >= ts, 1, -1)                 # +1=ราคาอยู่เหนือ TS (buy zone)
    flipped = np.zeros(n, dtype=bool)
    flipped[1:] = dr[1:] != dr[:-1]              # แท่งที่ราคาตัดผ่าน TS

    return {"ts": ts, "direction": dr, "flipped": flipped}


def utbot_signal(df, key_value: float = 1.0, atr_period: int = 10,
                 fresh_bars: int = 2, sl_atr_mult: float = 0.3) -> dict:
    """สัญญาณ UT Bot — ตรวจราคาตัดผ่าน Trailing Stop ล่าสุด
    fresh_bars: 2 (ไวกว่า SuperTrend/HalfTrend — เหมาะ M15/H1)
    SL = Trailing Stop ± ATR buffer"""
    if df is None or len(df) < atr_period + 10:
        return {"detected": False}
    res = utbot(df, key_value, atr_period)
    if res.get("ts") is None:
        return {"detected": False}

    dr_arr, flip_arr, ts_arr = res["direction"], res["flipped"], res["ts"]
    flip_bar = 0
    for i in range(1, min(fresh_bars + 1, len(flip_arr) + 1)):
        if flip_arr[-i]:
            flip_bar = i; break
    if not flip_bar:
        return {"detected": False}

    direction = "buy" if int(dr_arr[-1]) == 1 else "sell"
    c = float(df["close"].iloc[-1])
    ts_val = float(ts_arr[-1])
    atr_val = _atr(df)

    if direction == "buy":
        sl = round(ts_val - sl_atr_mult * atr_val, 5)
        if sl >= c: return {"detected": False}
    else:
        sl = round(ts_val + sl_atr_mult * atr_val, 5)
        if sl <= c: return {"detected": False}

    return {"detected": True, "direction": direction, "entry": round(c, 5),
            "sl": sl, "ts_value": round(ts_val, 5), "flip_bars_ago": flip_bar,
            "reason": f"UT Bot crossover {flip_bar} แท่งที่แล้ว → {direction.upper()} (TS={round(ts_val, 4)})"}


# ─────────────────────────────────────────────────────────────────────────────
# #5 EMA Ribbon Scalp (M5) — กลยุทธ์ scalp 5-10 นาที ระดับ prop firm
#
# ใช้โดย: SMB Capital, T3 Trading, Warrior Trading (New York prop desks)
# หลักการ: EMA 8 ตัด EMA 21 = momentum shift + RSI ยืนยัน momentum zone
#   - เข้าเมื่อ EMA fast ตัด EMA slow ใน N แท่งล่าสุด
#   - RSI 40-65 = "momentum zone" (มีแรงพอ แต่ไม่ extreme)
#   - ไม่รอ H1 flip → ตอบสนองเร็ว เหมาะ 5-10 นาที
#   - SL: 1×ATR(M5) ใต้/เหนือ EMA slow (แน่นกว่า H1 strategies)
#   - TP: 2×ATR(M5) หรือ ปรับตาม TP_ATR_MULT ใน config
# ─────────────────────────────────────────────────────────────────────────────

def ema_ribbon_signal(df, fast: int = 8, slow: int = 21,
                      rsi_min: float = 40.0, rsi_max: float = 65.0,
                      fresh_bars: int = 3, sl_atr_mult: float = 1.0) -> dict:
    """EMA 8/21 Cross Signal (M5) — กลยุทธ์ scalp ระดับ professional
    ใช้ใน prop firms: EMA fast ตัด slow + RSI momentum zone (40-65)
    fresh_bars: รับสัญญาณที่เกิดใน N แท่งล่าสุด (กัน stale signal)
    sl_atr_mult: SL = EMA slow ± N×ATR (default 1.0 = แน่นเหมาะ scalp)"""
    import numpy as np
    if df is None or len(df) < slow + fresh_bars + 15:
        return {"detected": False}

    close = df["close"].astype(float).values
    high  = df["high"].astype(float).values
    low   = df["low"].astype(float).values

    # ── EMA fast/slow (Wilder / pandas-style exponential) ─────────────────
    alpha_f = 2.0 / (fast + 1)
    alpha_s = 2.0 / (slow + 1)
    ef = np.empty(len(close)); ef[0] = close[0]
    es = np.empty(len(close)); es[0] = close[0]
    for i in range(1, len(close)):
        ef[i] = alpha_f * close[i] + (1 - alpha_f) * ef[i - 1]
        es[i] = alpha_s * close[i] + (1 - alpha_s) * es[i - 1]

    # ── หา EMA cross ใน fresh_bars แท่งล่าสุด ──────────────────────────
    direction = None
    for i in range(1, fresh_bars + 2):
        if i + 1 >= len(ef):
            break
        was_bull = ef[-i - 1] > es[-i - 1]  # แท่งก่อนหน้า
        now_bull = ef[-i] > es[-i]           # แท่งที่ i จากปัจจุบัน
        if not was_bull and now_bull:
            direction = "buy"; break
        if was_bull and not now_bull:
            direction = "sell"; break
    if direction is None:
        return {"detected": False}

    # ── RSI(14) — ต้องอยู่ใน momentum zone ────────────────────────────
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    avg_up = np.convolve(up, np.ones(14) / 14, mode="valid")[-1]
    avg_dn = np.convolve(dn, np.ones(14) / 14, mode="valid")[-1]
    rsi = 100.0 - 100.0 / (1.0 + avg_up / avg_dn) if avg_dn > 0 else 50.0
    if not (rsi_min <= rsi <= rsi_max):
        return {"detected": False}

    # ── ATR(14) ────────────────────────────────────────────────────────
    prev_c = np.roll(close, 1); prev_c[0] = close[0]
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    atr = float(tr[-14:].mean()) if len(tr) >= 14 else float(tr.mean())

    cur = close[-1]
    es_val = es[-1]   # EMA slow = base สำหรับ SL
    if direction == "buy":
        sl = round(min(es_val, cur) - sl_atr_mult * atr, 5)
        if sl >= cur:
            return {"detected": False}
    else:
        sl = round(max(es_val, cur) + sl_atr_mult * atr, 5)
        if sl <= cur:
            return {"detected": False}

    return {
        "detected": True,
        "direction": direction,
        "entry": round(cur, 5),
        "sl": sl,
        "atr": round(atr, 5),
        "rsi": round(rsi, 1),
        "ema_fast": round(float(ef[-1]), 5),
        "ema_slow": round(float(es_val), 5),
        "reason": f"EMA {fast}/{slow} cross → {direction.upper()} · RSI {rsi:.0f}",
    }


def vpoc(df, bin_atr_mult: float = 0.03) -> dict:
    """
    Volume Profile จาก OHLCV DataFrame → หา VPOC + Value Area (VAH/VAL)

    ใช้ tick_volume เป็น proxy ของ real volume (Forex/CFD บน Exness) — correlation ~90%
    bin_atr_mult: ขนาด bin = ATR × mult — auto-scale ตาม instrument
                  (ทอง ~$0.5 · Crypto ~$50 · FX ~0.0005)

    คืน {vpoc, vah, val, total_vol, bin_size} หรือ {} ถ้าข้อมูลไม่พอ

    VAH/VAL = Value Area ครอบ 70% ของ volume รอบ VPOC
    → ราคาใน VAL–VAH = ตลาดสมดุล (ยังไม่มีทิศ) · นอก VAH/VAL = directional move
    """
    if df is None or len(df) < 20:
        return {}

    # ดึง volume column — MT5 ให้ tick_volume เป็น proxy ของ traded volume
    vol_col = ("tick_volume" if "tick_volume" in df.columns
               else "volume" if "volume" in df.columns else None)
    if vol_col is None:
        return {}

    vols  = df[vol_col].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows  = df["low"].to_numpy(dtype=float)

    atr_val = _atr(df, 14)
    if atr_val <= 0:
        return {}
    bin_size = atr_val * bin_atr_mult
    if bin_size <= 0:
        return {}

    min_price = float(lows.min())
    max_price = float(highs.max())
    n_bins = max(1, int((max_price - min_price) / bin_size) + 2)

    # กระจาย volume ของแต่ละแท่งอย่างสม่ำเสมอตาม price range (low→high)
    bins = np.zeros(n_bins, dtype=float)
    for i in range(len(df)):
        lo_b = max(0, int((lows[i]  - min_price) / bin_size))
        hi_b = min(n_bins - 1, int((highs[i] - min_price) / bin_size))
        n_span   = max(1, hi_b - lo_b + 1)
        vol_per  = vols[i] / n_span
        bins[lo_b : hi_b + 1] += vol_per

    total_vol = float(bins.sum())
    if total_vol <= 0:
        return {}

    # VPOC = bin ที่มี volume สูงสุด (Point of Control)
    vpoc_bin   = int(np.argmax(bins))
    vpoc_price = min_price + (vpoc_bin + 0.5) * bin_size

    # Value Area: ขยายจาก VPOC ออกทั้งสองทาง จนครอบ 70% ของ total volume
    # (ตามหลัก Market Profile — Chicago Board of Trade)
    va_target = total_vol * 0.70
    lo_va, hi_va = vpoc_bin, vpoc_bin
    va_vol = float(bins[vpoc_bin])

    while va_vol < va_target:
        lo_ext = float(bins[lo_va - 1]) if lo_va > 0         else 0.0
        hi_ext = float(bins[hi_va + 1]) if hi_va < n_bins - 1 else 0.0
        if lo_ext == 0.0 and hi_ext == 0.0:
            break
        # extend ฝั่งที่ volume มากกว่า (Market Profile convention)
        if lo_ext >= hi_ext and lo_va > 0:
            lo_va -= 1;  va_vol += lo_ext
        elif hi_va < n_bins - 1:
            hi_va += 1;  va_vol += hi_ext
        else:
            break

    return {
        "vpoc":      round(vpoc_price,                          8),
        "vah":       round(min_price + (hi_va + 1) * bin_size, 8),
        "val":       round(min_price + lo_va * bin_size,        8),
        "total_vol": int(total_vol),
        "bin_size":  round(bin_size, 8),
    }


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


def hybrid_pro(df, rr: float = 2.5, rsi_lo: float = 40, rsi_hi: float = 60,
               sl_atr_mult: float = 0.5) -> dict:
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
        sl = round(float(df["low"].iloc[-6:].min()) - sl_atr_mult * atr, 5)
        if sl < c:
            return {"detected": True, "direction": "buy", "entry": round(c, 5),
                    "sl": sl, "tp": round(c + rr * (c - sl), 5),
                    "reason": "Hybrid-Pro: H1 ขาขึ้น + ย่อ EMA20 + RSI 40-60 + แท่งกลับตัว"}
    if dn and hi >= e20 - 0.3 * atr and rsi_lo <= rsi <= rsi_hi and _bear_trigger(df):
        sl = round(float(df["high"].iloc[-6:].max()) + sl_atr_mult * atr, 5)
        if sl > c:
            return {"detected": True, "direction": "sell", "entry": round(c, 5),
                    "sl": sl, "tp": round(c - rr * (sl - c), 5),
                    "reason": "Hybrid-Pro: H1 ขาลง + เด้ง EMA20 + RSI 40-60 + แท่งกลับตัว"}
    return {"detected": False}


# ═══════════════════════════════════════════════════════════════════════════════
# SCALP SUITE — 4 กลยุทธ์ระดับ prop firm / institutional (5-10 นาที)
# ═══════════════════════════════════════════════════════════════════════════════

def vwap_bounce_signal(df, std_entry: float = 0.5, std_sl: float = 1.5,
                       rsi_period: int = 14, rsi_min: float = 35.0,
                       rsi_max: float = 65.0) -> dict:
    """VWAP Bounce Signal — Jane Street / Citadel institutional style

    ราคายุติธรรมของวัน (VWAP) = จุดที่สถาบันมักรับ/ขาย
    - ราคาวิ่งออกห่าง VWAP > std_entry × σ แล้ว pullback กลับมาแตะ VWAP
    - แท่งปัจจุบันเด้งออกจาก VWAP band → เข้า
    - SL: std_sl × σ อีกฝั่ง (ถ้า VWAP ถูกทะลุจริงๆ สัญญาณเสีย)

    df: M5 OHLCV ตั้งแต่ต้นวัน (แนะนำ 300 bars — ให้ VWAP มีข้อมูลเพียงพอ)
    Returns: {detected, direction, entry, sl, vwap, std, rsi, reason}"""
    _E = {"detected": False}
    try:
        if not hasattr(df, "columns"):
            df = pd.DataFrame(df)
        if len(df) < 20:
            return _E

        df = df.copy()
        # แปลง time → UTC datetime สำหรับ daily reset
        if not pd.api.types.is_datetime64_any_dtype(df["time"]):
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["date"] = df["time"].dt.date
        today = df["date"].max()
        ddf = df[df["date"] == today].copy()
        if len(ddf) < 5:
            return _E          # VWAP ต้องการข้อมูลวันนี้ — fallback multi-day ให้ VWAP ผิด

        # VWAP = Σ(typical_price × volume) / Σ(volume) — reset ทุกวัน
        ddf["tp"]  = (ddf["high"] + ddf["low"] + ddf["close"]) / 3.0
        vol_col    = "tick_volume" if "tick_volume" in ddf.columns else "volume"
        ddf["vol"] = ddf[vol_col].replace(0, 1).astype(float)
        ddf["vwap"] = (ddf["tp"] * ddf["vol"]).cumsum() / ddf["vol"].cumsum()

        # σ = rolling std ของ (typical_price - VWAP) — วัด "ห่างแค่ไหน"
        win = min(20, max(5, len(ddf) // 2))
        ddf["sigma"] = (ddf["tp"] - ddf["vwap"]).rolling(win, min_periods=3).std().bfill()
        ddf["sigma"] = ddf["sigma"].clip(lower=1e-10)

        cur_vwap   = float(ddf["vwap"].iloc[-1])
        cur_sigma  = float(ddf["sigma"].iloc[-1])
        cur_close  = float(ddf["close"].iloc[-1])
        prev_close = float(ddf["close"].iloc[-2]) if len(ddf) >= 2 else cur_close

        # สัญญาณ bounce: ราคาเคยอยู่นอก band แล้วกลับมาข้าม band กลับ
        upper = cur_vwap + std_entry * cur_sigma
        lower = cur_vwap - std_entry * cur_sigma
        direction = None
        if prev_close > upper and cur_close <= upper:
            direction = "sell"
        elif prev_close < lower and cur_close >= lower:
            direction = "buy"
        if not direction:
            return _E

        # Volume confirmation — bounce ต้องมี volume รองรับ ไม่ใช่เด้งในตลาดเงียบ
        # tick_volume ใช้แทน real volume ได้ (broker Forex/CFD: correlation ~90%)
        _vols_v  = ddf["vol"]                          # normalize แล้ว (0 → 1 แล้ว ผ่าน replace)
        _cur_vol = float(_vols_v.iloc[-1])
        _avg_vol = float(_vols_v.rolling(20, min_periods=5).mean().iloc[-1])
        if _avg_vol > 1 and _cur_vol < _avg_vol * 1.3:
            return {**_E, "reason": f"volume {_cur_vol:.0f} < MA20×1.3={_avg_vol * 1.3:.0f} — false bounce"}

        # RSI — กัน overbought/oversold ที่อาจทำให้ false bounce
        closes = df["close"].values.astype(float)
        rp = min(rsi_period, len(closes) - 2)
        if rp < 3:
            return _E
        d = np.diff(closes[-(rp + 2):])
        ag = np.where(d > 0, d, 0.0)[-rp:].mean()
        al = np.where(d < 0, -d, 0.0)[-rp:].mean()
        rsi = 100.0 - (100.0 / (1.0 + ag / al)) if al > 1e-12 else 50.0
        if not (rsi_min <= rsi <= rsi_max):
            return {**_E, "reason": f"RSI {rsi:.0f} ออกนอก zone {rsi_min}-{rsi_max}"}

        entry = cur_close
        sl = (cur_vwap - std_sl * cur_sigma) if direction == "buy" else (cur_vwap + std_sl * cur_sigma)
        return {
            "detected":  True,
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "vwap":      cur_vwap,
            "std":       cur_sigma,
            "rsi":       rsi,
            "reason":    f"VWAP bounce {direction} · RSI {rsi:.0f} · σ={cur_sigma:.5f}",
        }
    except Exception:  # noqa: BLE001
        return _E


def bb_squeeze_signal(df, bb_period: int = 20, bb_std_mult: float = 2.0,
                      squeeze_lookback: int = 50,
                      volume_min_mult: float = 1.2) -> dict:
    """Bollinger Band Squeeze Breakout — momentum hedge fund style

    BB แคบลง (volatility ต่ำ = ตลาดพักตัว) → จับ momentum burst ที่ออกมา
    - Squeeze: BB width ต่ำสุดใน squeeze_lookback bars ใน 3 bars ล่าสุด
    - Breakout: ราคาทะลุ upper/lower BB ทันทีหลัง squeeze
    - Volume ยืนยัน: volume > avg × volume_min_mult (กัน false breakout)
    - SL: BB midline ± ATR (กลางแถบ → ถ้าทะลุกลับ สัญญาณเสีย)

    Returns: {detected, direction, entry, sl, bb_upper, bb_lower, bb_mid, width, atr, reason}"""
    _E = {"detected": False}
    try:
        if not hasattr(df, "columns"):
            df = pd.DataFrame(df)
        n = len(df)
        if n < bb_period + squeeze_lookback + 5:
            return _E

        closes = df["close"].values.astype(float)
        highs  = df["high"].values.astype(float)
        lows   = df["low"].values.astype(float)
        vol_col = "tick_volume" if "tick_volume" in df.columns else "volume"
        vols   = df[vol_col].values.astype(float)

        # BB: rolling mean + std
        mids   = np.array([closes[i - bb_period:i].mean() for i in range(bb_period, n + 1)])
        stds_v = np.array([closes[i - bb_period:i].std()  for i in range(bb_period, n + 1)])
        uppers = mids + bb_std_mult * stds_v
        lowers = mids - bb_std_mult * stds_v
        widths = (uppers - lowers) / (np.abs(mids) + 1e-10)

        if len(widths) < squeeze_lookback + 3:
            return _E

        # squeeze ต้องเกิดใน 3 bars ล่าสุด (ก่อน current bar)
        hist_w   = widths[-(squeeze_lookback + 3):-2]
        recent_w = widths[-3:-1]
        if recent_w.min() > hist_w.min():
            return {**_E, "reason": "ไม่มี BB squeeze ใน recent bars"}

        # ตรวจ breakout
        cur_c = closes[-1]; prev_c2 = closes[-2]
        cu = uppers[-1]; cl_bb = lowers[-1]; cm = mids[-1]
        pu = uppers[-2]; pl = lowers[-2]
        direction = None
        if cur_c > cu and prev_c2 <= pu:
            direction = "buy"
        elif cur_c < cl_bb and prev_c2 >= pl:
            direction = "sell"
        if not direction:
            return {**_E, "reason": "ราคายังใน BB"}

        # volume ยืนยัน
        avg_vol = vols[-(bb_period + 2):-1].mean()
        if avg_vol > 0 and vols[-1] < avg_vol * volume_min_mult:
            return {**_E, "reason": f"volume {vols[-1]:.0f} ไม่ยืนยัน (avg {avg_vol:.0f})"}

        # ATR
        pc = np.roll(closes, 1); pc[0] = closes[0]
        tr = np.maximum(highs - lows, np.maximum(np.abs(highs - pc), np.abs(lows - pc)))
        atr = tr[-14:].mean()

        entry = cur_c
        sl = (cm - atr) if direction == "buy" else (cm + atr)
        return {
            "detected":  True,
            "direction": direction,
            "entry":     entry,
            "sl":        sl,
            "bb_upper":  cu,
            "bb_lower":  cl_bb,
            "bb_mid":    cm,
            "width":     float(widths[-1]),
            "atr":       atr,
            "reason":    f"BB squeeze breakout {direction} · width={widths[-1]:.4f}",
        }
    except Exception:  # noqa: BLE001
        return _E


def rsi_divergence_signal(df, rsi_period: int = 14, lookback: int = 30,
                           swing_strength: int = 2) -> dict:
    """RSI Divergence (Bearish/Bullish) M5 — discretionary trader standard

    จับจุดกลับตัวก่อนคนอื่น: ราคาไปต่อ แต่ momentum ลดลง
    - Bearish div: ราคา Higher High + RSI Lower High → แรงซื้อหมด → Short
    - Bullish div: ราคา Lower Low + RSI Higher Low → แรงขายหมด → Long
    - swing_strength: จำนวน bars ทั้ง 2 ข้างที่ต้องต่ำ/สูงกว่า pivot

    Returns: {detected, direction, entry, sl, div_type, rsi, atr, reason}"""
    _E = {"detected": False}
    try:
        if not hasattr(df, "columns"):
            df = pd.DataFrame(df)
        closes = df["close"].values.astype(float)
        highs  = df["high"].values.astype(float)
        lows   = df["low"].values.astype(float)
        n = len(closes)
        if n < rsi_period + lookback + 10:
            return _E

        # RSI series — Wilder's smoothing (เหมือน MT5 ใช้)
        rsi_arr = np.full(n, 50.0)
        d = np.diff(closes)
        g = np.where(d > 0, d, 0.0)
        l_arr = np.where(d < 0, -d, 0.0)
        ag = g[:rsi_period].mean()
        al = l_arr[:rsi_period].mean()
        for i in range(rsi_period, len(d)):
            ag = (ag * (rsi_period - 1) + g[i]) / rsi_period
            al = (al * (rsi_period - 1) + l_arr[i]) / rsi_period
            rsi_arr[i + 1] = 100.0 - (100.0 / (1.0 + ag / al)) if al > 1e-12 else 100.0

        # หา swing highs/lows บน scope ล่าสุด
        s = swing_strength
        scope = lookback + s + 2
        sh  = highs[-scope:]
        sl3 = lows[-scope:]
        sr  = rsi_arr[-scope:]
        m2  = len(sh)

        sh_list, sl_list = [], []
        for i in range(s, m2 - s - 1):
            if all(sh[i] >= sh[i - j] for j in range(1, s + 1)) and \
               all(sh[i] >= sh[i + j] for j in range(1, s + 1)):
                sh_list.append((i, sh[i], sr[i]))
            if all(sl3[i] <= sl3[i - j] for j in range(1, s + 1)) and \
               all(sl3[i] <= sl3[i + j] for j in range(1, s + 1)):
                sl_list.append((i, sl3[i], sr[i]))

        direction = None; div_type = ""; rsi_val = rsi_arr[-1]

        # Bearish: ราคา HH + RSI LH
        if len(sh_list) >= 2:
            p1, p2 = sh_list[-2], sh_list[-1]
            if p2[1] > p1[1] and p2[2] < p1[2] and (m2 - p2[0]) <= 6:
                direction = "sell"; div_type = "bearish"

        # Bullish: ราคา LL + RSI HL
        if not direction and len(sl_list) >= 2:
            p1, p2 = sl_list[-2], sl_list[-1]
            if p2[1] < p1[1] and p2[2] > p1[2] and (m2 - p2[0]) <= 6:
                direction = "buy"; div_type = "bullish"

        if not direction:
            return {**_E, "reason": "ไม่เจอ divergence"}

        pc = np.roll(closes, 1); pc[0] = closes[0]
        tr  = np.maximum(highs - lows, np.maximum(np.abs(highs - pc), np.abs(lows - pc)))
        atr = tr[-14:].mean()

        entry = closes[-1]
        sl4 = (entry - atr * 1.5) if direction == "buy" else (entry + atr * 1.5)
        return {
            "detected":  True,
            "direction": direction,
            "entry":     entry,
            "sl":        sl4,
            "div_type":  div_type,
            "rsi":       rsi_val,
            "atr":       atr,
            "reason":    f"{div_type} divergence · RSI={rsi_val:.1f}",
        }
    except Exception:  # noqa: BLE001
        return _E


def orb_session_signal(df, session: str = "london", range_bars: int = 3,
                       trade_window_min: int = 90, now=None) -> dict:
    """Opening Range Breakout — Toby Crabel (1990) ใช้ใน hedge fund 30+ ปี

    สร้าง opening range จาก N bars แรกของ session แล้วรอทะลุ
    - London open: 07:00 UTC · NY open (FX): 13:00 UTC · US cash open: 13:30 UTC
    - now: ฉีดเวลาได้สำหรับ test (default = เวลาปัจจุบัน UTC)
    - range_bars × 5 นาที = ความกว้างกรอบ (3 bars = 15 นาที)
    - trade_window_min: ปิดโอกาสหลัง N นาที (กัน false breakout ตอนบ่าย)
    - กัน late entry: bars ก่อนทะลุไปแล้ว → ไม่เข้า

    df: M5 OHLCV ตั้งแต่ก่อน session open (แนะนำ 200 bars)
    Returns: {detected, direction, entry, sl, range_high, range_low, range_size, atr, reason}"""
    _E = {"detected": False}
    try:
        from datetime import datetime as _dt, timezone as _tz
        # (ชั่วโมง, นาที) UTC — "us" = ตลาดหุ้น/ดัชนี US เปิดจริง 13:30 UTC (20:30 ไทย summer)
        _SESSION_HM = {"london": (7, 0), "ny": (13, 0), "us": (13, 30), "asia": (0, 0), "tokyo": (0, 0)}
        open_h, open_m = _SESSION_HM.get(session.lower(), (7, 0))

        if not hasattr(df, "columns"):
            df = pd.DataFrame(df)
        if len(df) < range_bars + 5:
            return _E

        df = df.copy()
        if not pd.api.types.is_datetime64_any_dtype(df["time"]):
            df["utc"] = pd.to_datetime(df["time"], unit="s", utc=True)
        else:
            df["utc"] = df["time"].dt.tz_localize("UTC") if df["time"].dt.tz is None else df["time"]

        now_utc   = now or _dt.now(_tz.utc)
        open_utc  = now_utc.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        # ใช้ datetime.timedelta แทน pd.Timedelta เพราะ open_utc เป็น datetime.datetime
        # pd.Timedelta + datetime → pd.Timestamp ซึ่งเปรียบต่างกับ datetime ได้แต่ fragile
        from datetime import timedelta as _td
        close_utc = open_utc + _td(minutes=trade_window_min)

        # ตรวจ trade window
        if not (open_utc <= now_utc <= close_utc):
            return {**_E, "reason": f"นอกหน้าต่าง {session} open"}

        # Opening range
        range_end = open_utc + _td(minutes=range_bars * 5)
        rng = df[(df["utc"] >= open_utc) & (df["utc"] < range_end)]
        if len(rng) < range_bars:
            return {**_E, "reason": f"รอ opening range ({len(rng)}/{range_bars} bars)"}

        range_high = float(rng["high"].max())
        range_low  = float(rng["low"].min())
        range_size = range_high - range_low

        closes = df["close"].values.astype(float)
        highs  = df["high"].values.astype(float)
        lows   = df["low"].values.astype(float)
        pc = np.roll(closes, 1); pc[0] = closes[0]
        tr = np.maximum(highs - lows, np.maximum(np.abs(highs - pc), np.abs(lows - pc)))
        atr = tr[-14:].mean()

        if atr > 0 and range_size < atr * 0.3:
            return {**_E, "reason": f"range {range_size:.5f} เล็กเกิน"}

        post = df[df["utc"] >= range_end]
        if len(post) == 0:
            return {**_E, "reason": "รอ bars หลัง opening range"}

        # กัน late entry
        if len(post) > 1:
            prev_p = post.iloc[:-1]
            if (prev_p["close"] > range_high).any() or (prev_p["close"] < range_low).any():
                return {**_E, "reason": "ทะลุ range ไปแล้ว (late entry)"}

        cur_c   = float(post["close"].iloc[-1])
        prev_c2 = float(post["close"].iloc[-2]) if len(post) >= 2 else float(df["close"].iloc[-2])

        direction = None
        if cur_c > range_high and prev_c2 <= range_high:
            direction = "buy"
        elif cur_c < range_low and prev_c2 >= range_low:
            direction = "sell"
        if not direction:
            return {**_E, "reason": f"ราคา {cur_c:.5f} ยังใน range"}

        entry = cur_c
        sl5 = (range_low - atr * 0.3) if direction == "buy" else (range_high + atr * 0.3)
        return {
            "detected":   True,
            "direction":  direction,
            "entry":      entry,
            "sl":         sl5,
            "range_high": range_high,
            "range_low":  range_low,
            "range_size": range_size,
            "atr":        atr,
            "reason":     f"ORB {session} {direction} · range={range_size:.5f}",
        }
    except Exception:  # noqa: BLE001
        return _E
