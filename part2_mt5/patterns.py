"""
part2_mt5/patterns.py — ทรงกราฟ + โครงสร้างเทรนด์ + volume (ยืนยันก่อนเข้า)

ทำงานบน DataFrame OHLC — ไม่พึ่ง MT5/Part 1
เน้นของที่ "เทรดเดอร์สั้นใช้จริง + คำนวณได้น่าเชื่อถือ":
- volume เริ่มเข้า (แรงจริง vs หลอก)
- เบรกกรอบ (breakout จาก consolidation)
- โครงสร้าง HH/HL vs LH/LL
- ย่อแตะ EMA แล้วเด้ง (pullback ต่อเทรนด์)
"""
from __future__ import annotations
from typing import Optional


def volume_entering(df, lookback: int = 20) -> dict:
    """volume เริ่มเข้า = วอลุ่มแท่งล่าสุดมากกว่าค่าเฉลี่ย AND กำลังไต่ขึ้น
    คืน {ratio, entering, rising} — ratio>1.5 + entering = แรงเข้าของจริง (ยืนยันเบรก)"""
    v = df["volume"].astype(float)
    if len(v) < max(lookback, 3):
        return {"ratio": None, "entering": False, "rising": False}
    avg = v.iloc[-lookback:].mean()
    cur = float(v.iloc[-1])
    ratio = cur / avg if avg > 0 else None
    rising = bool(v.iloc[-1] > v.iloc[-2] and v.iloc[-1] >= v.iloc[-3:].mean())
    entering = bool(ratio is not None and ratio >= 1.2 and rising)
    return {"ratio": round(ratio, 2) if ratio else None, "entering": entering, "rising": rising}


def breakout(df, lookback: int = 20) -> Optional[dict]:
    """เบรกกรอบ: ปิดเหนือ high สูงสุด / ใต้ low ต่ำสุด ของ N แท่งก่อนหน้า
    คืน {type(breakout_up/down), level} หรือ None"""
    if len(df) < lookback + 2:
        return None
    prior = df.iloc[-lookback - 1:-1]
    hi, lo = float(prior["high"].max()), float(prior["low"].min())
    c = float(df["close"].iloc[-1])
    if c > hi:
        return {"type": "breakout_up", "level": hi}
    if c < lo:
        return {"type": "breakout_down", "level": lo}
    return None


def structure(df, lookback: int = 20, swing: int = 3) -> dict:
    """โครงสร้างเทรนด์จาก swing high/low: HH+HL=ขึ้น, LH+LL=ลง, อื่น=ไม่ชัด
    swing = จำนวนแท่งซ้าย-ขวาที่ใช้ยืนยันจุด swing"""
    seg = df.iloc[-lookback:] if len(df) >= lookback else df
    highs, lows = seg["high"].astype(float).to_list(), seg["low"].astype(float).to_list()
    sh, sl = [], []
    for i in range(swing, len(seg) - swing):
        if highs[i] == max(highs[i - swing:i + swing + 1]):
            sh.append(highs[i])
        if lows[i] == min(lows[i - swing:i + swing + 1]):
            sl.append(lows[i])
    up = len(sh) >= 2 and len(sl) >= 2 and sh[-1] > sh[0] and sl[-1] > sl[0]
    down = len(sh) >= 2 and len(sl) >= 2 and sh[-1] < sh[0] and sl[-1] < sl[0]
    label = "ขาขึ้น (HH/HL)" if up else "ขาลง (LH/LL)" if down else "ไม่ชัด/ออกข้าง"
    return {"trend": "up" if up else "down" if down else "side", "label": label}


def _ohlc(row) -> tuple[float, float, float, float]:
    return float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])


def three_bar_play(df, direction: str, atr: Optional[float] = None) -> dict:
    """3-Bar Play (กลยุทธ์ตามน้ำความเร็วสูง — momentum continuation):
      แท่ง1 = จุดชนวนใหญ่ (ตามทิศ) → แท่ง2 = พัก 1-2 แท่งเล็ก (ห้ามย่อหลุดครึ่งแท่ง1)
      → แท่ง3 = เบรกจุดสูง(buy)/ต่ำ(sell) ของแท่ง1
    คืน {detected, entry, sl, n_rest, bar1_range, reason} · SL = ใต้/เหนือแท่งพัก (แคบ = R:R ดี)
    buy = เข้าตามขาขึ้น · sell = เข้าตามขาลง (มิเรอร์กัน)"""
    if df is None or len(df) < 5:
        return {"detected": False}
    is_buy = direction == "buy"
    if atr is None or atr <= 0:                       # ATR อ้างอิงขนาดแท่ง (fallback = range เฉลี่ย)
        rng = (df["high"].astype(float) - df["low"].astype(float))
        atr = float(rng.iloc[-20:].mean()) if len(rng) >= 5 else float(rng.mean())
    if atr <= 0:
        return {"detected": False}

    for n_rest in (1, 2):                             # พักได้ 1 หรือ 2 แท่ง
        if len(df) < n_rest + 2:
            continue
        o1, h1, l1, c1 = _ohlc(df.iloc[-(n_rest + 2)])    # แท่ง1 = จุดชนวน
        rng1 = h1 - l1
        if rng1 <= 0:
            continue
        big = rng1 >= 1.3 * atr and abs(c1 - o1) >= 0.5 * rng1   # ใหญ่ + ตัวหนา
        dir_ok = (c1 > o1) if is_buy else (c1 < o1)             # ทิศถูก (เขียว/แดง)
        if not (big and dir_ok):
            continue
        mid1 = l1 + 0.5 * rng1                         # ครึ่งหนึ่งของแท่ง1
        rest = df.iloc[-(n_rest + 1):-1]               # แท่งพัก (ระหว่างแท่ง1 กับแท่งล่าสุด)
        rest_lows, rest_highs, ok = [], [], True
        for _, r in rest.iterrows():
            _, rh, rl, _ = _ohlc(r)
            small = (rh - rl) <= 0.7 * rng1            # เล็กกว่าแท่ง1
            hold = (rl >= mid1) if is_buy else (rh <= mid1)   # ไม่ย่อหลุดครึ่งแท่ง1
            if not (small and hold):
                ok = False
                break
            rest_lows.append(rl)
            rest_highs.append(rh)
        if not ok or not rest_lows:
            continue
        _, h3, l3, _ = _ohlc(df.iloc[-1])              # แท่ง3 = แท่งล่าสุด
        broke = (h3 > h1) if is_buy else (l3 < l1)     # เบรกจุดสูง/ต่ำของแท่ง1
        if not broke:
            continue
        entry = h1 if is_buy else l1
        sl = min(rest_lows) if is_buy else max(rest_highs)
        return {"detected": True, "entry": round(entry, 5), "sl": round(sl, 5),
                "n_rest": n_rest, "bar1_range": round(rng1, 5),
                "reason": f"3-Bar Play {'ขึ้น' if is_buy else 'ลง'} (แท่งใหญ่+พัก{n_rest}+เบรก)"}
    return {"detected": False}


def pullback_to_ema(df, ema_series, atr: Optional[float] = None) -> dict:
    """ย่อแตะ EMA แล้วเด้ง (จังหวะเข้าต่อเทรนด์ ไม่ไล่ของแพง)
    ema_series = pandas Series ของ EMA (เช่น EMA20) ยาวเท่ากับ df"""
    try:
        ema = float(ema_series.iloc[-1])
        c, lo, hi = float(df["close"].iloc[-1]), float(df["low"].iloc[-1]), float(df["high"].iloc[-1])
    except Exception:  # noqa: BLE001
        return {"near": False}
    tol = (atr * 0.5) if atr else c * 0.01
    touched = lo <= ema + tol  # ราคาลงไปแตะโซน EMA
    holding = c > ema           # แต่ปิดยืนเหนือ EMA (เด้งกลับ)
    return {"near": bool(touched and holding), "ema": ema}


def _atr_of(df, n: int = 20) -> float:
    rng = (df["high"].astype(float) - df["low"].astype(float))
    return float(rng.iloc[-n:].mean()) if len(rng) >= 5 else float(rng.mean())


def support_resistance(df, lookback: int = 60, swing: int = 3, atr: Optional[float] = None) -> dict:
    """หาแนวรับ-ต้านใกล้สุดจาก swing highs/lows + เช็กว่าราคาปัจจุบัน 'ชน' แนวไหน
    คืน {resistance, support, near_resistance, near_support} — ใช้เตือน 'ไม่ long ชนต้าน/short ชนรับ'"""
    if df is None or len(df) < swing * 2 + 3:
        return {"resistance": None, "support": None, "near_resistance": False, "near_support": False}
    seg = df.iloc[-lookback:] if len(df) >= lookback else df
    highs, lows = seg["high"].astype(float).tolist(), seg["low"].astype(float).tolist()
    sh, sl = [], []
    for i in range(swing, len(seg) - swing):
        if highs[i] == max(highs[i - swing:i + swing + 1]):
            sh.append(highs[i])
        if lows[i] == min(lows[i - swing:i + swing + 1]):
            sl.append(lows[i])
    c = float(df["close"].iloc[-1])
    atr = atr if (atr and atr > 0) else _atr_of(df)
    tol = atr * 0.5 if atr > 0 else c * 0.004
    res = min([h for h in sh if h >= c], default=None)    # แนวต้านเหนือราคา ใกล้สุด
    sup = max([l for l in sl if l <= c], default=None)    # แนวรับใต้ราคา ใกล้สุด
    return {
        "resistance": round(res, 5) if res is not None else None,
        "support": round(sup, 5) if sup is not None else None,
        "near_resistance": bool(res is not None and (res - c) <= tol),
        "near_support": bool(sup is not None and (c - sup) <= tol),
    }


def breakout_retest(df, direction: str, atr: Optional[float] = None,
                    lookback: int = 40, swing: int = 3) -> dict:
    """Breakout & Retest (ตามเทรนด์): เบรกแนว → ย่อกลับมาทดสอบแนวเดิม (retest) → แท่งกลับตัวยืนยัน
      buy = เบรกแนวต้านขึ้นแล้ว retest · sell = เบรกแนวรับลงแล้ว retest
    คืน {detected, level, entry, sl, reason} · SL = ใต้/เหนือแนวที่เพิ่งเบรก"""
    import candles
    if df is None or len(df) < 12:
        return {"detected": False}
    is_buy = direction == "buy"
    atr = atr if (atr and atr > 0) else _atr_of(df)
    if atr <= 0:
        return {"detected": False}
    tol = atr * 0.5
    seg = df.iloc[-lookback:] if len(df) >= lookback else df
    highs, lows, closes = (seg["high"].astype(float).tolist(),
                           seg["low"].astype(float).tolist(),
                           seg["close"].astype(float).tolist())
    n = len(seg)
    # แนว (swing) ในส่วน "เก่า" ของหน้าต่าง (เว้น 4 แท่งล่าสุดไว้เป็น retest/trigger)
    levels = []
    for i in range(swing, n - swing - 4):
        if is_buy and highs[i] == max(highs[i - swing:i + swing + 1]):
            levels.append(highs[i])
        if (not is_buy) and lows[i] == min(lows[i - swing:i + swing + 1]):
            levels.append(lows[i])
    if not levels:
        return {"detected": False}
    rev = [p for p in candles.detect(df) if p["dir"] == ("bull" if is_buy else "bear")]
    if not rev:                                  # ต้องมีแท่งกลับตัวยืนยันที่ retest
        return {"detected": False}
    c = float(df["close"].iloc[-1])
    rc, rlo, rhi = closes[-6:], min(lows[-4:]), max(highs[-4:])
    for L in sorted(set(levels), reverse=is_buy):
        if is_buy:
            if max(rc) > L + 0.1 * atr and rlo <= L + tol and c >= L - tol:    # เบรก+retest
                cand_sl = round(min(rlo, L) - 0.3 * atr, 5)
                if cand_sl < c:
                    return {"detected": True, "level": round(L, 5), "entry": round(c, 5),
                            "sl": cand_sl, "reason": f"เบรกแนวต้าน {round(L, 2)} แล้ว retest + แท่งกลับตัว"}
        else:
            if min(rc) < L - 0.1 * atr and rhi >= L - tol and c <= L + tol:
                cand_sl = round(max(rhi, L) + 0.3 * atr, 5)
                if cand_sl > c:
                    return {"detected": True, "level": round(L, 5), "entry": round(c, 5),
                            "sl": cand_sl, "reason": f"เบรกแนวรับ {round(L, 2)} แล้ว retest + แท่งกลับตัว"}
    return {"detected": False}


def inside_bar_breakout(df, direction: str, atr: Optional[float] = None) -> dict:
    """Inside Bar Breakout (สปริงดีดตัว): Mother Bar ใหญ่ → Inside Bar เล็ก 1-2 แท่งในกรอบ
      → แท่งล่าสุดเบรกจุดสูง(buy)/ต่ำ(sell) ของ Mother. SL ใต้/เหนือ Inside Bar
    คืน {detected, entry, sl, mother_range, n_inside, reason}"""
    if df is None or len(df) < 4:
        return {"detected": False}
    is_buy = direction == "buy"
    for n_in in (1, 2):                              # Inside bar 1 หรือ 2 แท่ง
        if len(df) < n_in + 2:
            continue
        mo = df.iloc[-(n_in + 2)]                     # Mother Bar
        mh, ml = float(mo["high"]), float(mo["low"])
        if mh <= ml:
            continue
        inside = df.iloc[-(n_in + 1):-1]
        ins_h, ins_l = inside["high"].astype(float), inside["low"].astype(float)
        if not ((ins_h <= mh).all() and (ins_l >= ml).all()):   # ต้องอยู่ในกรอบ Mother
            continue
        h3, l3 = float(df["high"].iloc[-1]), float(df["low"].iloc[-1])
        if is_buy and h3 > mh:
            return {"detected": True, "entry": round(mh, 5), "sl": round(float(ins_l.min()), 5),
                    "mother_range": round(mh - ml, 5), "n_inside": n_in,
                    "reason": f"Inside Bar Breakout ขึ้น (เบรก Mother {round(mh, 2)})"}
        if (not is_buy) and l3 < ml:
            return {"detected": True, "entry": round(ml, 5), "sl": round(float(ins_h.max()), 5),
                    "mother_range": round(mh - ml, 5), "n_inside": n_in,
                    "reason": f"Inside Bar Breakout ลง (เบรก Mother {round(ml, 2)})"}
    return {"detected": False}


def two_legged_pullback(df, direction: str, atr: Optional[float] = None,
                        lookback: int = 30, swing: int = 2) -> dict:
    """2-Legged Pullback (ย่อ 2 จังหวะตามเทรนด์ใหญ่ — Brooks/Volman):
      เทรนด์ขึ้น → ย่อ Leg1 → เด้ง → ย่อ Leg2 → แท่งเขียวสวนกลับ = เข้า buy (มิเรอร์สำหรับ sell)
    คืน {detected, sl, n_legs, reason} · SL = ใต้/เหนือจุดต่ำ/สูงของ Leg2"""
    import candles
    if df is None or len(df) < 12:
        return {"detected": False}
    is_buy = direction == "buy"
    seg = df.iloc[-lookback:] if len(df) >= lookback else df
    highs, lows = seg["high"].astype(float).tolist(), seg["low"].astype(float).tolist()
    n = len(seg)
    sh_idx = [i for i in range(swing, n - swing) if highs[i] == max(highs[i - swing:i + swing + 1])]
    sl_idx = [i for i in range(swing, n - swing) if lows[i] == min(lows[i - swing:i + swing + 1])]
    rev = [p for p in candles.detect(df) if p["dir"] == ("bull" if is_buy else "bear")]
    if not rev:                                      # ต้องมีแท่งสวนกลับยืนยัน
        return {"detected": False}
    if is_buy:
        legs = [i for i in sl_idx if i >= n // 3]    # swing low 2 จุด = Leg1, Leg2 (ในพักตัว)
        if len(legs) < 2:
            return {"detected": False}
        leg1, leg2 = legs[-2], legs[-1]
        if not [i for i in sh_idx if leg1 < i < leg2]:   # ต้องมีเด้ง (swing high) คั่นกลาง 2 leg
            return {"detected": False}
        return {"detected": True, "sl": round(min(lows[leg2], lows[-1]), 5), "n_legs": 2,
                "reason": "2-Legged Pullback ขึ้น (ย่อ 2 จังหวะ + เด้งเขียว)"}
    else:
        legs = [i for i in sh_idx if i >= n // 3]
        if len(legs) < 2:
            return {"detected": False}
        leg1, leg2 = legs[-2], legs[-1]
        if not [i for i in sl_idx if leg1 < i < leg2]:
            return {"detected": False}
        return {"detected": True, "sl": round(max(highs[leg2], highs[-1]), 5), "n_legs": 2,
                "reason": "2-Legged Pullback ลง (เด้ง 2 จังหวะ + แท่งแดง)"}
