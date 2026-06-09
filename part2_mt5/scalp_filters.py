"""
part2_mt5/scalp_filters.py — Pro Scalping Filters

รวม 4 filter ที่ scalper มืออาชีพใช้จริง:
  1. Kill Zone   — เข้าเฉพาะช่วงเวลา volume สูง (ICT)
  2. Liquidity Sweep — รอกวาด stop ก่อนเข้า (SMC/ICT)
  3. Momentum Confirm — แท่งที่ 2 ยืนยันทิศ (Tom Hougaard)
  4. VWAP Distance   — ราคาห่าง VWAP พอดี ไม่ไกลเกิน (Prop Firm)

ใช้งาน:
  from scalp_filters import check_all_filters
  result = check_all_filters(df_m5, direction, cfg)
  if not result["pass"]:
      return {"skipped": True, "reason": result["reason"]}
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("part2.scalp_filters")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. KILL ZONE FILTER (ICT)
# เข้าเฉพาะช่วงที่ institutional players active
# London: 07:00-09:00 UTC
# New York: 13:00-15:30 UTC
# London Close: 15:00-16:00 UTC
# Asian (low vol): 23:00-06:00 UTC → ห้ามเข้า
# ═══════════════════════════════════════════════════════════════════════════════

_KILL_ZONES = {
    "london_open":    (7,  0, 9,  0),   # London Open Kill Zone
    "ny_open":        (13, 0, 15, 30),  # New York Open Kill Zone
    "london_close":   (15, 0, 16, 0),  # London Close (volatile)
}

_DEAD_ZONES = [
    (23, 0, 24, 0),   # Asian dead (pre-midnight UTC)
    (0,  0, 6, 30),   # Asian dead (early morning UTC)
]


def in_kill_zone(now: Optional[datetime] = None,
                 zones: Optional[list[str]] = None) -> tuple[bool, str]:
    """
    คืน (True, ชื่อ zone) ถ้าตอนนี้อยู่ใน kill zone
    zones=None → เช็คทุก zone
    """
    now = now or datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    now_min = h * 60 + m

    want = zones or list(_KILL_ZONES.keys())
    for name in want:
        if name not in _KILL_ZONES:
            continue
        sh, sm, eh, em = _KILL_ZONES[name]
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= now_min < end:
            label = {
                "london_open":  "London Open (07-09 UTC)",
                "ny_open":      "NY Open (13-15:30 UTC)",
                "london_close": "London Close (15-16 UTC)",
            }.get(name, name)
            return True, label

    return False, ""


def in_dead_zone(now: Optional[datetime] = None) -> tuple[bool, str]:
    """คืน (True, reason) ถ้าอยู่ใน dead zone (volume ต่ำมาก)"""
    now = now or datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    now_min = h * 60 + m

    for sh, sm, eh, em in _DEAD_ZONES:
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= now_min < end:
            return True, f"Dead zone {sh:02d}:{sm:02d}-{eh:02d}:{em:02d} UTC (volume ต่ำ)"
    return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. LIQUIDITY SWEEP DETECTION (ICT / SMC)
# รอราคากวาด stop ของฝั่งตรงข้ามก่อน แล้วค่อยกลับ
# Buy setup: ราคา sweep low (กวาด stop ของ longs ที่ผิด) แล้วกลับขึ้น
# Sell setup: ราคา sweep high (กวาด stop ของ shorts ที่ผิด) แล้วกลับลง
# ═══════════════════════════════════════════════════════════════════════════════

def detect_liquidity_sweep(
    df: pd.DataFrame,
    direction: str,
    lookback: int = 20,
    sweep_bars: int = 3,
    atr: Optional[float] = None,
) -> dict:
    """
    ตรวจ Liquidity Sweep:
    - Buy: ราคาแทงต่ำสุดเคย sweep low ของ lookback แท่งก่อน
            แล้วปิดกลับขึ้นมาเหนือ swing low (rejection)
    - Sell: กลับด้าน

    คืน {detected, sweep_level, rejection_strength, bars_ago}
    """
    if df is None or len(df) < lookback + sweep_bars + 2:
        return {"detected": False, "reason": "ข้อมูลไม่พอ"}

    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    closes = df["close"].astype(float).values

    if atr is None:
        pc = np.roll(closes, 1); pc[0] = closes[0]
        tr = np.maximum(highs - lows, np.maximum(np.abs(highs - pc), np.abs(lows - pc)))
        atr = float(tr[-20:].mean())

    n = len(df)

    if direction == "buy":
        # หา swing low ใน lookback แท่งก่อน (ไม่รวม sweep_bars ล่าสุด)
        ref_segment = lows[-(lookback + sweep_bars):-sweep_bars]
        if len(ref_segment) == 0:
            return {"detected": False, "reason": "ข้อมูลไม่พอ"}
        swing_low = float(np.min(ref_segment))

        # ตรวจว่า sweep_bars ล่าสุดมีแท่งที่หลุดใต้ swing_low
        recent_lows = lows[-sweep_bars:]
        swept = any(l < swing_low for l in recent_lows)
        if not swept:
            return {"detected": False, "reason": f"ยังไม่มี sweep low {swing_low:.5f}"}

        # แท่งล่าสุดต้องปิดกลับขึ้นมาเหนือ swing_low (rejection / reversal)
        cur_close = closes[-1]
        cur_low = lows[-1]
        rejection = cur_close > swing_low and cur_low < swing_low

        # strength = ระยะ rejection เทียบ ATR
        strength = (cur_close - cur_low) / atr if atr > 0 else 0

        if rejection and strength >= 0.3:
            bars_ago = int(np.argmin(recent_lows[-sweep_bars:]))
            return {
                "detected": True,
                "sweep_level": round(swing_low, 5),
                "rejection_strength": round(strength, 2),
                "bars_ago": bars_ago,
                "reason": f"Liquidity sweep low {swing_low:.5f} → rejection {strength:.2f}×ATR",
            }
        return {"detected": False, "reason": f"Sweep เกิดแล้วแต่ rejection อ่อน ({strength:.2f}×ATR < 0.3)"}

    else:  # sell
        ref_segment = highs[-(lookback + sweep_bars):-sweep_bars]
        if len(ref_segment) == 0:
            return {"detected": False, "reason": "ข้อมูลไม่พอ"}
        swing_high = float(np.max(ref_segment))

        recent_highs = highs[-sweep_bars:]
        swept = any(h > swing_high for h in recent_highs)
        if not swept:
            return {"detected": False, "reason": f"ยังไม่มี sweep high {swing_high:.5f}"}

        cur_close = closes[-1]
        cur_high = highs[-1]
        rejection = cur_close < swing_high and cur_high > swing_high

        strength = (cur_high - cur_close) / atr if atr > 0 else 0

        if rejection and strength >= 0.3:
            bars_ago = int(np.argmax(recent_highs[-sweep_bars:]))
            return {
                "detected": True,
                "sweep_level": round(swing_high, 5),
                "rejection_strength": round(strength, 2),
                "bars_ago": bars_ago,
                "reason": f"Liquidity sweep high {swing_high:.5f} → rejection {strength:.2f}×ATR",
            }
        return {"detected": False, "reason": f"Sweep เกิดแล้วแต่ rejection อ่อน ({strength:.2f}×ATR < 0.3)"}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. MOMENTUM CONFIRMATION (Tom Hougaard / Linda Raschke)
# ต้องมีแท่งที่ 2 ยืนยันทิศก่อนเข้า
# + ADX > threshold (มีเทรนด์พอให้ scalp)
# + แท่งยืนยันต้องมี body ชัดเจน (ไม่ใช่ doji)
# ═══════════════════════════════════════════════════════════════════════════════

def check_momentum_confirmation(
    df: pd.DataFrame,
    direction: str,
    adx_threshold: float = 20.0,
    min_body_ratio: float = 0.4,
    confirm_bars: int = 2,
) -> dict:
    """
    ตรวจ Momentum Confirmation:
    - ADX ต้องเหนือ threshold (มีแรงพอ)
    - แท่ง confirm_bars ล่าสุดต้องเป็นทิศเดียวกัน
    - body ratio ต้องไม่น้อยเกิน (ไม่ใช่ doji/spinning top)
    - แท่งล่าสุดปิดในครึ่งบน (buy) หรือครึ่งล่าง (sell) ของ range

    คืน {confirmed, adx, body_ratio, reason}
    """
    if df is None or len(df) < max(14, confirm_bars) + 5:
        return {"confirmed": False, "reason": "ข้อมูลไม่พอ"}

    opens  = df["open"].astype(float).values
    highs  = df["high"].astype(float).values
    lows   = df["low"].astype(float).values
    closes = df["close"].astype(float).values

    # ADX
    pc = np.roll(closes, 1); pc[0] = closes[0]
    up_m = np.diff(highs); up_m = np.where(up_m > 0, up_m, 0)
    dn_m = -np.diff(lows);  dn_m = np.where(dn_m > 0, dn_m, 0)
    tr_full = np.maximum(highs[1:] - lows[1:],
                         np.maximum(np.abs(highs[1:] - closes[:-1]),
                                    np.abs(lows[1:]  - closes[:-1])))
    period = 14
    if len(tr_full) < period:
        return {"confirmed": False, "reason": "ข้อมูลไม่พอคำนวณ ADX"}

    atr14 = tr_full[-period:].mean()
    pdi = up_m[-period:].mean() / atr14 * 100 if atr14 > 0 else 0
    mdi = dn_m[-period:].mean() / atr14 * 100 if atr14 > 0 else 0
    dx  = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
    adx_val = float(dx)  # simplified ADX

    if adx_val < adx_threshold:
        return {
            "confirmed": False,
            "adx": round(adx_val, 1),
            "reason": f"ADX {adx_val:.1f} < {adx_threshold} (ตลาด sideways ไม่มีแรง)",
        }

    # ตรวจแท่ง confirm_bars ล่าสุด
    recent_o = opens[-confirm_bars:]
    recent_c = closes[-confirm_bars:]
    recent_h = highs[-confirm_bars:]
    recent_l = lows[-confirm_bars:]

    if direction == "buy":
        # แท่งทั้งหมดต้องปิดสูงกว่าเปิด (bullish)
        all_bullish = all(c > o for c, o in zip(recent_c, recent_o))
        # แท่งล่าสุดปิดในครึ่งบนของ range
        last_range = recent_h[-1] - recent_l[-1]
        close_pos = (recent_c[-1] - recent_l[-1]) / last_range if last_range > 0 else 0.5
        in_upper_half = close_pos >= 0.5
        # body ratio
        body = abs(recent_c[-1] - recent_o[-1])
        body_ratio = body / last_range if last_range > 0 else 0
        strong_body = body_ratio >= min_body_ratio

        if all_bullish and in_upper_half and strong_body:
            return {
                "confirmed": True,
                "adx": round(adx_val, 1),
                "body_ratio": round(body_ratio, 2),
                "reason": f"Momentum confirmed: ADX {adx_val:.1f}, body {body_ratio:.0%}, close upper half",
            }
        reasons = []
        if not all_bullish:    reasons.append("แท่งไม่ bullish ครบ")
        if not in_upper_half:  reasons.append(f"ปิดล่าง range ({close_pos:.0%})")
        if not strong_body:    reasons.append(f"body อ่อน ({body_ratio:.0%} < {min_body_ratio:.0%})")
        return {"confirmed": False, "adx": round(adx_val, 1), "reason": " | ".join(reasons)}

    else:  # sell
        all_bearish = all(c < o for c, o in zip(recent_c, recent_o))
        last_range = recent_h[-1] - recent_l[-1]
        close_pos = (recent_c[-1] - recent_l[-1]) / last_range if last_range > 0 else 0.5
        in_lower_half = close_pos <= 0.5
        body = abs(recent_c[-1] - recent_o[-1])
        body_ratio = body / last_range if last_range > 0 else 0
        strong_body = body_ratio >= min_body_ratio

        if all_bearish and in_lower_half and strong_body:
            return {
                "confirmed": True,
                "adx": round(adx_val, 1),
                "body_ratio": round(body_ratio, 2),
                "reason": f"Momentum confirmed: ADX {adx_val:.1f}, body {body_ratio:.0%}, close lower half",
            }
        reasons = []
        if not all_bearish:    reasons.append("แท่งไม่ bearish ครบ")
        if not in_lower_half:  reasons.append(f"ปิดบน range ({close_pos:.0%})")
        if not strong_body:    reasons.append(f"body อ่อน ({body_ratio:.0%} < {min_body_ratio:.0%})")
        return {"confirmed": False, "adx": round(adx_val, 1), "reason": " | ".join(reasons)}


# ═══════════════════════════════════════════════════════════════════════════════
# 4. VWAP DISTANCE FILTER (Prop Firm Standard)
# ราคาต้องอยู่ในระยะที่เหมาะกับ VWAP
# ใกล้เกิน = ยังไม่มีทิศ (ไม่เข้า)
# ไกลเกิน = mean reversion risk สูง (ไม่เข้า)
# ═══════════════════════════════════════════════════════════════════════════════

def check_vwap_distance(
    df: pd.DataFrame,
    direction: str,
    min_dist_atr: float = 0.3,   # ต้องออกห่าง VWAP อย่างน้อย N×ATR
    max_dist_atr: float = 3.0,   # ห้ามไกลเกิน N×ATR (mean reversion risk)
) -> dict:
    """
    ตรวจระยะจาก VWAP:
    - ราคาต้องอยู่ฝั่งถูกต้องของ VWAP (buy = เหนือ VWAP, sell = ใต้)
    - ไม่ชิด VWAP เกิน (sideway zone)
    - ไม่ไกล VWAP เกิน (extended / mean reversion risk)

    คืน {pass, vwap, distance_atr, side_ok, reason}
    """
    if df is None or len(df) < 20:
        return {"pass": True, "reason": "ข้อมูลไม่พอตรวจ VWAP — ผ่านไปก่อน"}

    # คำนวณ VWAP (intraday reset ถ้ามี time column)
    closes = df["close"].astype(float)
    highs  = df["high"].astype(float)
    lows   = df["low"].astype(float)
    vol_col = "tick_volume" if "tick_volume" in df.columns else "volume"

    if vol_col in df.columns:
        vols = df[vol_col].astype(float).replace(0, 1)
    else:
        vols = pd.Series(np.ones(len(df)), index=df.index)

    tp = (highs + lows + closes) / 3.0
    vwap = float((tp * vols).cumsum().iloc[-1] / vols.cumsum().iloc[-1])

    # ATR
    pc = closes.shift(1).fillna(closes)
    tr = pd.concat([highs - lows, (highs - pc).abs(), (lows - pc).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())
    if atr <= 0:
        return {"pass": True, "reason": "ATR=0 — ผ่านไปก่อน"}

    cur = float(closes.iloc[-1])
    dist = cur - vwap               # บวก = เหนือ VWAP, ลบ = ใต้ VWAP
    dist_atr = dist / atr           # normalized

    # ฝั่งถูก
    side_ok = (dist_atr > 0) if direction == "buy" else (dist_atr < 0)
    abs_dist = abs(dist_atr)

    if not side_ok:
        side_word = "ใต้" if direction == "buy" else "เหนือ"
        return {
            "pass": False,
            "vwap": round(vwap, 5),
            "distance_atr": round(dist_atr, 2),
            "reason": f"ราคาอยู่{side_word} VWAP (สวนทาง {direction}) — ไม่เข้า",
        }

    if abs_dist < min_dist_atr:
        return {
            "pass": False,
            "vwap": round(vwap, 5),
            "distance_atr": round(dist_atr, 2),
            "reason": f"ชิด VWAP เกิน ({abs_dist:.2f}×ATR < {min_dist_atr}) — sideways zone",
        }

    if abs_dist > max_dist_atr:
        return {
            "pass": False,
            "vwap": round(vwap, 5),
            "distance_atr": round(dist_atr, 2),
            "reason": f"ไกล VWAP เกิน ({abs_dist:.2f}×ATR > {max_dist_atr}) — mean reversion risk",
        }

    return {
        "pass": True,
        "vwap": round(vwap, 5),
        "distance_atr": round(dist_atr, 2),
        "reason": f"VWAP distance OK: {dist_atr:+.2f}×ATR",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER FUNCTION — รวมทุก filter
# ═══════════════════════════════════════════════════════════════════════════════

def check_all_filters(
    df: pd.DataFrame,
    direction: str,
    cfg: dict,
    *,
    symbol: str = "",
    now: Optional[datetime] = None,
    atr: Optional[float] = None,
) -> dict:
    """
    รัน Pro Scalping Filters ทั้งหมด
    คืน {pass, score, max_score, filters, reason, details}

    cfg keys (ทั้งหมดมี default — ไม่ต้องตั้งก็ใช้ได้):
      SCALP_FILTER_KILL_ZONE=true      เข้าเฉพาะ London/NY Kill Zone
      SCALP_FILTER_DEAD_ZONE=true      บล็อก Asian dead zone
      SCALP_FILTER_LIQ_SWEEP=true      ต้องมี liquidity sweep ก่อน
      SCALP_FILTER_LIQ_REQUIRED=false  false=เป็น bonus ไม่บังคับ
      SCALP_FILTER_MOMENTUM=true       ต้องมี momentum confirm
      SCALP_FILTER_VWAP=true           ตรวจ VWAP distance
      SCALP_FILTER_MIN_SCORE=2         ผ่านได้ถ้า score >= N (จาก filters ที่เปิด)
      SCALP_ADX_MIN=20                 ADX ขั้นต่ำ
      SCALP_KILL_ZONES=london_open,ny_open  zones ที่ต้องการ (comma-separated)
      SCALP_LIQ_LOOKBACK=20            แท่งย้อนหลังหา swing สำหรับ liquidity
      SCALP_LIQ_SWEEP_BARS=3           แท่งล่าสุดที่ตรวจ sweep
      SCALP_VWAP_MIN_DIST=0.3          ระยะ min จาก VWAP (×ATR)
      SCALP_VWAP_MAX_DIST=3.0          ระยะ max จาก VWAP (×ATR)
      SCALP_MOMENTUM_BARS=2            จำนวนแท่งยืนยัน
      SCALP_BODY_RATIO=0.4             body ratio ขั้นต่ำ
    """
    def _bool(key: str, default: bool) -> bool:
        return cfg.get(key, str(default)).lower() in ("1", "true", "yes", "on")

    def _float(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (ValueError, TypeError):
            return default

    def _int(key: str, default: int) -> int:
        try:
            return int(cfg.get(key, default))
        except (ValueError, TypeError):
            return default

    use_kz       = _bool("SCALP_FILTER_KILL_ZONE",  True)
    use_dz       = _bool("SCALP_FILTER_DEAD_ZONE",  True)
    use_liq      = _bool("SCALP_FILTER_LIQ_SWEEP",  True)
    liq_required = _bool("SCALP_FILTER_LIQ_REQUIRED", False)  # false = bonus เท่านั้น
    use_mom      = _bool("SCALP_FILTER_MOMENTUM",   True)
    use_vwap     = _bool("SCALP_FILTER_VWAP",       True)
    min_score    = _int("SCALP_FILTER_MIN_SCORE",   2)

    adx_min     = _float("SCALP_ADX_MIN",          20.0)
    liq_lb      = _int("SCALP_LIQ_LOOKBACK",       20)
    liq_sb      = _int("SCALP_LIQ_SWEEP_BARS",     3)
    vwap_min    = _float("SCALP_VWAP_MIN_DIST",    0.3)
    vwap_max    = _float("SCALP_VWAP_MAX_DIST",    3.0)
    mom_bars    = _int("SCALP_MOMENTUM_BARS",       2)
    body_ratio  = _float("SCALP_BODY_RATIO",        0.4)

    zones_raw = cfg.get("SCALP_KILL_ZONES", "london_open,ny_open")
    kill_zones = [z.strip() for z in zones_raw.split(",") if z.strip()]

    details = {}
    score = 0
    max_score = 0
    hard_fail = None   # filter ที่บังคับ fail ทันที

    now = now or datetime.now(timezone.utc)

    # ── 1. Dead Zone (hard block เสมอ ถ้าเปิด) ──────────────────────
    if use_dz:
        dead, dead_reason = in_dead_zone(now)
        details["dead_zone"] = {"blocked": dead, "reason": dead_reason or "ไม่ใช่ dead zone"}
        if dead:
            return {
                "pass": False,
                "score": 0,
                "max_score": 0,
                "filters": details,
                "reason": f"🚫 Dead Zone: {dead_reason}",
            }

    # ── 2. Kill Zone ───────────────────────────────────────────────
    if use_kz:
        max_score += 1
        in_kz, kz_name = in_kill_zone(now, kill_zones)
        details["kill_zone"] = {"pass": in_kz, "zone": kz_name or "นอก Kill Zone"}
        if in_kz:
            score += 1
        else:
            hard_fail = f"⏰ นอก Kill Zone — ตอนนี้ {now.strftime('%H:%M')} UTC (ต้องการ: {zones_raw})"

    # ── 3. Liquidity Sweep ─────────────────────────────────────────
    if use_liq:
        if liq_required:
            max_score += 2   # required = น้ำหนักมากขึ้น
        else:
            max_score += 1   # bonus
        liq = detect_liquidity_sweep(df, direction, liq_lb, liq_sb, atr)
        details["liquidity_sweep"] = liq
        if liq.get("detected"):
            score += 2 if liq_required else 1
        elif liq_required:
            hard_fail = hard_fail or f"💧 ไม่มี Liquidity Sweep: {liq.get('reason', '')}"

    # ── 4. Momentum Confirmation ───────────────────────────────────
    if use_mom:
        max_score += 1
        mom = check_momentum_confirmation(df, direction, adx_min, body_ratio, mom_bars)
        details["momentum"] = mom
        if mom.get("confirmed"):
            score += 1
        else:
            hard_fail = hard_fail or f"📊 Momentum อ่อน: {mom.get('reason', '')}"

    # ── 5. VWAP Distance ───────────────────────────────────────────
    if use_vwap:
        max_score += 1
        vwap_r = check_vwap_distance(df, direction, vwap_min, vwap_max)
        details["vwap"] = vwap_r
        if vwap_r.get("pass"):
            score += 1
        else:
            hard_fail = hard_fail or f"📉 VWAP: {vwap_r.get('reason', '')}"

    # ── ตัดสิน ─────────────────────────────────────────────────────
    passed = score >= min_score

    if not passed and hard_fail:
        reason = hard_fail
    elif not passed:
        reason = f"Score {score}/{max_score} < {min_score} (ผ่านไม่พอ filter)"
    else:
        reason = f"✅ Pro filters passed: {score}/{max_score}"

    # log รายละเอียดถ้าไม่ผ่าน
    if not passed and symbol:
        log.info("Scalp filter FAIL %s %s — %s", symbol, direction, reason)

    return {
        "pass": passed,
        "score": score,
        "max_score": max_score,
        "filters": details,
        "reason": reason,
    }
