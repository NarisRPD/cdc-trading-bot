"""
core/signals.py — CDC Action Zone V3 (piriya33) + confluence score

หลักสำคัญ (อย่าลืม):
1. ใช้แท่งที่ "ปิดแล้ว" เท่านั้น — caller ต้องตัดแท่งปัจจุบันที่ยังก่อตัวออกก่อน
2. 4 โซน = (EMA12 vs EMA26) × (close vs EMA12)
3. สัญญาณยิงเมื่อ "แท่งล่าสุดเขียว แต่แท่งก่อนหน้าไม่ใช่เขียว" (Buy)
   หรือ "แท่งล่าสุดแดง แต่แท่งก่อนหน้าไม่ใช่แดง" (Sell)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional
import pandas as pd

from .indicators import ema, sma, rsi, adx, atr

SignalType = Literal["buy", "sell", "none"]
# 6 โซนตาม CDC ActionZone V3 (piriya33)
Zone = Literal["green", "yellow", "orange", "red", "lblue", "blue", "unknown"]

# ชื่อ + ความหมาย (ตามต้นฉบับ: เหลือง/ส้ม = ขาขึ้นแต่ย่อ, ฟ้า = ขาลงแต่เด้ง)
_ZONE_LABEL = {
    "green":  "🟢 เขียว (ขาขึ้น-ถือ/ซื้อ)",
    "yellow": "🟡 เหลือง (ขาขึ้นเริ่มย่อ)",
    "orange": "🟠 เหลืองเข้ม (ปลายขาขึ้น-ย่อแรง)",
    "red":    "🔴 แดง (ขาลง-ถือ/ขาย)",
    "lblue":  "🩵 ฟ้าอ่อน (ขาลงเริ่มเด้ง)",
    "blue":   "🔵 ฟ้าเข้ม (ปลายขาลง-เด้งแรง)",
}


def zone_label(zone: Optional[str]) -> str:
    """โซน → emoji + ชื่อ + ความหมาย"""
    return _ZONE_LABEL.get(zone or "", "—")


def _rsi_desc(rsi_val: float, is_buy: bool) -> str:
    """คำอธิบายโมเมนตัมจากค่า RSI (ภาษาคน)"""
    if is_buy:
        if rsi_val >= 65:
            return "โมเมนตัมแข็งแกร่ง"
        if rsi_val >= 55:
            return "โมเมนตัมเป็นบวก"
        if rsi_val >= 45:
            return "โมเมนตัมเป็นกลาง"
        return "โมเมนตัมอ่อน"
    else:
        if rsi_val <= 35:
            return "โมเมนตัมลงแรง"
        if rsi_val <= 45:
            return "โมเมนตัมเป็นลบ"
        if rsi_val <= 55:
            return "โมเมนตัมเป็นกลาง"
        return "ยังมีแรงซื้อต้าน"


@dataclass
class Signal:
    symbol: str
    display_name: str       # ชื่อสำหรับโชว์ในข้อความ (เช่น "Gold (GC=F)")
    signal: SignalType
    zone: Zone
    close: float
    score: int              # 0–4
    high_quality: bool      # score ≥ 3 และผ่าน trend filter
    bar_date: pd.Timestamp  # วันที่แท่งปิดอ้างอิง
    notes: list[str]        # filter ไหนผ่าน/ไม่ผ่าน — ไว้ debug
    mtf_aligned: Optional[bool] = None  # สัญญาณตรงเทรนด์รายสัปดาห์ไหม (None=ไม่รู้)
    breakdown: list[dict] = field(default_factory=list)  # [{ok: bool, text: str}] ไว้โชว์ ✅/❌
    atr: Optional[float] = None  # ATR(14) ล่าสุด — ไว้คำนวณคาดการณ์เป้าราคา
    adx: Optional[float] = None  # ADX(14) ล่าสุด — ไว้คาดคะเนความเร็วถึงเป้า + hard filter
    rsi: Optional[float] = None  # RSI(14) ล่าสุด — ไว้ hard filter overbought/oversold
    vol_above: Optional[bool] = None  # volume > SMA20 ไหม — ไว้ hard filter
    vol_ratio: Optional[float] = None  # volume / SMA20 (>1.5 = breakout แรง)
    prev_zone: Optional[str] = None  # โซนแท่งก่อนหน้า — บอกว่าสัญญาณมาจากไหน (ต่อเนื่อง/กลับตัว)
    rs_rank: Optional[float] = None  # Relative Strength percentile เทียบกลุ่ม (0-100) — แบบกองทุน momentum
    anticipate: Optional[str] = None  # "buy"/"sell" = ทิศที่ดักกลับตัว (สำหรับ reversal watch)
    option_liq: Optional[dict] = None  # สภาพคล่อง option (good/poor/unknown) — กันแนะนำตัวขายต่อยาก
    fund_flag: Optional[str] = None  # บรรทัดพื้นฐาน/นักวิเคราะห์ (Finnhub/FMP) — ใช้ใน scan กลุ่ม
    stage: Optional[dict] = None  # Weinstein Stage 1-4 {n,label,emoji,note,slope_pct,above} — ภาพใหญ่เทรนด์
    ema_fast: Optional[float] = None  # EMA12 ล่าสุด — ใช้คำนวณโซนราคาเข้าที่เหมาะ (แนวรับ/ต้าน)
    ema_slow: Optional[float] = None  # EMA26 ล่าสุด
    trend_q: Optional[dict] = None  # คุณภาพเทรนด์ {r2, up} — R² regression ราคา (เนียน vs ขรุขระ)

    def stars(self) -> str:
        return "⭐" * self.score + f" ({self.score}/4)" if self.score > 0 else "(0/4)"


def _zone_from_row(close_v: float, fast_v: float, slow_v: float) -> Zone:
    """
    6 โซน CDC ActionZone V3 (ตรงโค้ด piriya33 บรรทัด 66-72):
      Bull = fast>slow ; Bear = fast<slow ; price = close
      🟢 green  = Bull and price>fast
      🟡 yellow = Bull and price<fast and price>slow   (Dip / ย่อ)
      🟠 orange = Bull and price<fast and price<slow   (Strong Dip / ย่อแรง)
      🔴 red    = Bear and price<fast
      🩵 lblue  = Bear and price>fast and price<slow    (Rally / เด้ง)
      🔵 blue   = Bear and price>fast and price>slow    (Strong Rally / เด้งแรง)
    """
    if pd.isna(close_v) or pd.isna(fast_v) or pd.isna(slow_v):
        return "unknown"
    bull = fast_v > slow_v
    above_fast = close_v > fast_v
    above_slow = close_v > slow_v
    if bull:
        if above_fast:
            return "green"
        return "yellow" if above_slow else "orange"
    # bear
    if not above_fast:
        return "red"
    return "blue" if above_slow else "lblue"


# Weinstein Stage Analysis — ภาพใหญ่เทรนด์ (เสริม CDC zone ที่เป็นจังหวะสั้น)
_STAGE_INFO = {
    1: ("📊", "Stage 1 — สะสมฐาน", "ออกข้าง MA150 แบน · รอ breakout ยังไม่ใช่จังหวะซื้อ"),
    2: ("📈", "Stage 2 — ขาขึ้น", "ราคาเหนือ MA150 ที่ชี้ขึ้น ✅ ช่วงทอง น่าเล่นฝั่งซื้อ"),
    3: ("⚠️", "Stage 3 — ทำจุดสูง/แจกของ", "โมเมนตัมอ่อน เริ่มออกข้าง · ระวังกลับตัว"),
    4: ("🔻", "Stage 4 — ขาลง", "ราคาใต้ MA150 ที่ชี้ลง ❌ หลีกเลี่ยงฝั่งซื้อ"),
}


def weinstein_stage(close: pd.Series, ma_period: int = 150,
                    slope_lb: int = 20, slope_thr: float = 1.0) -> Optional[dict]:
    """จัด Weinstein Stage 1-4 จาก ราคา vs MA150(≈30 สัปดาห์) + ความชัน MA
      MA ชี้ขึ้น  → เหนือ MA = Stage 2 (markup) / ใต้ MA = Stage 1 (ฐานยกตัว)
      MA ชี้ลง   → ใต้ MA  = Stage 4 (markdown) / เหนือ MA = Stage 3 (distribution)
      MA แบน     → เหนือ MA = Stage 3 (ทำจุดสูง) / ใต้ MA = Stage 1 (สะสมฐาน)
    ต้องมี ≥ ma_period+slope_lb แท่ง ไม่งั้นคืน None (ข้อมูลไม่พอ)"""
    if close is None or len(close) < ma_period + slope_lb:
        return None
    ma = close.rolling(ma_period, min_periods=ma_period).mean()
    ma_now = ma.iloc[-1]
    ma_past = ma.iloc[-1 - slope_lb]
    if pd.isna(ma_now) or pd.isna(ma_past) or ma_past == 0:
        return None
    slope_pct = (ma_now - ma_past) / ma_past * 100.0
    above = float(close.iloc[-1]) > float(ma_now)
    if slope_pct > slope_thr:
        n = 2 if above else 1
    elif slope_pct < -slope_thr:
        n = 4 if not above else 3
    else:
        n = 3 if above else 1
    emoji, label, note = _STAGE_INFO[n]
    return {"n": n, "label": label, "emoji": emoji, "note": note,
            "slope_pct": round(slope_pct, 1), "above": above}


def trend_quality(close: pd.Series, n: int = 20) -> Optional[dict]:
    """คุณภาพเทรนด์ = R² ของ linear regression ราคา n แท่งล่าสุด (ราคาเกาะเส้นตรงแค่ไหน)
      R² สูง = เทรนด์เนียน (ขึ้น/ลงก็ได้) · R² ต่ำ = แกว่ง/ขรุขระ/sideway
    คืน {r2 (0-1), up (เครื่องหมายความชัน)} หรือ None ถ้าแท่งไม่พอ"""
    if close is None or len(close) < n:
        return None
    try:
        import numpy as np
        y = close.iloc[-n:].to_numpy(dtype=float)
        if np.allclose(y, y[0]):  # ราคาแบนสนิท = ไม่มีเทรนด์
            return {"r2": 0.0, "up": True}
        x = np.arange(n, dtype=float)
        r = float(np.corrcoef(x, y)[0, 1])  # R² = (Pearson r)² สำหรับ regression เชิงเส้น
        if np.isnan(r):
            return None
        return {"r2": round(r * r, 2), "up": r >= 0}
    except Exception:  # noqa: BLE001
        return None


def compute_signal(
    df: pd.DataFrame,
    symbol: str,
    display_name: Optional[str] = None,
    *,
    ema_fast: int = 12,
    ema_slow: int = 26,
    ema_trend: int = 200,
    adx_period: int = 14,
    rsi_period: int = 14,
    vol_sma_period: int = 20,
    adx_min: float = 20.0,          # C1: เกณฑ์ ADX ที่ให้ "ดาว momentum" — caller ส่ง min_adx_to_alert
    enable_ema200_filter: bool = True,
    min_bars_required: int = 60,
    enable_mtf: bool = True,
    score_when_none: bool = False,
    eval_direction: Optional[str] = None,
) -> Optional[Signal]:
    """
    df ต้องมีคอลัมน์ open/high/low/close/volume, index เป็น datetime
    และ "ตัดแท่งที่ยังไม่ปิดออกแล้ว" (caller รับผิดชอบ)
    คืน None ถ้าข้อมูลไม่พอ/พัง — ห้าม raise (เพื่อไม่ให้กลุ่มล่ม)
    """
    required_cols = {"close", "high", "low", "volume"}
    if df is None or df.empty or not required_cols.issubset(df.columns):
        return None

    df = df.dropna(subset=["close"]).copy()
    bars_needed = max(min_bars_required, ema_trend if enable_ema200_filter else ema_slow + 5)
    if len(df) < bars_needed:
        return None

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    fast = ema(close, ema_fast)
    slow = ema(close, ema_slow)
    trend = ema(close, ema_trend) if enable_ema200_filter else None
    adx_v = adx(high, low, close, adx_period)
    rsi_v = rsi(close, rsi_period)
    vol_avg = sma(volume, vol_sma_period)

    if pd.isna(fast.iloc[-1]) or pd.isna(slow.iloc[-1]):
        return None
    if len(df) < 2:
        return None

    zone_now = _zone_from_row(close.iloc[-1], fast.iloc[-1], slow.iloc[-1])
    zone_prev = _zone_from_row(close.iloc[-2], fast.iloc[-2], slow.iloc[-2])

    sig: SignalType = "none"
    if zone_now == "green" and zone_prev != "green":
        sig = "buy"
    elif zone_now == "red" and zone_prev != "red":
        sig = "sell"

    # คำนวณ confluence score ทุกครั้ง (ถึงไม่มีสัญญาณก็ไม่เสียหาย)
    score = 0
    notes: list[str] = []

    close_v = float(close.iloc[-1])
    trend_v = float(trend.iloc[-1]) if (trend is not None and not pd.isna(trend.iloc[-1])) else None
    adx_last = float(adx_v.iloc[-1]) if not pd.isna(adx_v.iloc[-1]) else None
    rsi_last = float(rsi_v.iloc[-1]) if not pd.isna(rsi_v.iloc[-1]) else None
    vol_last = float(volume.iloc[-1]) if not pd.isna(volume.iloc[-1]) else None
    vol_avg_last = float(vol_avg.iloc[-1]) if not pd.isna(vol_avg.iloc[-1]) else None

    atr_series = atr(df["high"], df["low"], close, 14)
    atr_last = float(atr_series.iloc[-1]) if not atr_series.empty and not pd.isna(atr_series.iloc[-1]) else None

    # ทิศที่ใช้ให้คะแนน confluence:
    #   มีสัญญาณ → ตามสัญญาณ
    #   ไม่มีสัญญาณ + score_when_none → อนุมานจากโซนปัจจุบัน (ใช้ใน /scan ตัวเดียว)
    #     bull (green/yellow/orange) = ฝั่งขึ้น · bear (red/lblue/blue) = ฝั่งลง
    if sig == "buy":
        eval_is_buy: Optional[bool] = True
    elif sig == "sell":
        eval_is_buy = False
    elif eval_direction == "buy":      # บังคับทิศ (reversal watch: ดักกลับตัวขึ้น)
        eval_is_buy = True
    elif eval_direction == "sell":     # ดักกลับตัวลง
        eval_is_buy = False
    elif score_when_none and zone_now in ("green", "yellow", "orange"):
        eval_is_buy = True
    elif score_when_none and zone_now in ("red", "lblue", "blue"):
        eval_is_buy = False
    else:
        eval_is_buy = None

    trend_pass = False
    breakdown: list[dict] = []
    if eval_is_buy is not None:
        is_buy = eval_is_buy

        # 1) Trend (เทียบ EMA200)
        if trend_v is not None:
            ok = close_v > trend_v if is_buy else close_v < trend_v
            if ok:
                score += 1
                trend_pass = True
            if is_buy:
                text = "อยู่เหนือ EMA200 (เทรนด์ใหญ่ขาขึ้น)" if ok else "อยู่ใต้ EMA200 (สวนเทรนด์ใหญ่)"
            else:
                text = "อยู่ใต้ EMA200 (เทรนด์ใหญ่ขาลง)" if ok else "อยู่เหนือ EMA200 (สวนเทรนด์ใหญ่)"
            notes.append("trend" + (">" if is_buy else "<") + "EMA200")
            breakdown.append({"ok": ok, "text": text})

        # 2) Momentum (ADX) — ดาวให้ตามเกณฑ์เดียวกับ hard filter (C1) กัน "ดาว phantom"
        #    ที่ได้ตอน 20<ADX≤25 แต่ถูก gate ตัดทิ้ง → 'ดาว/HQ' ไม่ตรงกับสิ่งที่ผู้ใช้เห็น
        if adx_last is not None:
            ok = adx_last > adx_min
            if ok:
                score += 1
            text = f"ADX = {adx_last:.0f} (" + ("มีโมเมนตัมจริง" if ok else "ตลาด sideways") + ")"
            notes.append(f"ADX={adx_last:.1f}")
            breakdown.append({"ok": ok, "text": text})

        # 3) Volume ยืนยัน — ดาวให้เฉพาะ "breakout แรงจริง" (≥1.5× SMA20)
        #    กัน double-count: vol>SMA20 เป็น hard gate (require_volume_above_sma) อยู่แล้ว
        #    ถ้านับ vol>SMA20 เป็นดาวด้วย ไม้ที่รอด gate จะได้ดาวนี้ทุกตัว → ดาวไร้ค่าแยกแยะ + ดัน HQ เกินจริง
        if vol_last is not None and vol_avg_last is not None:
            ratio = (vol_last / vol_avg_last) if vol_avg_last > 0 else None
            above = vol_last > vol_avg_last                     # ผ่าน gate ปริมาณ
            ok = ratio is not None and ratio >= 1.5             # ดาว = breakout แรง (discriminating)
            if ok:
                score += 1
            if ratio is None:               # vol เฉลี่ย=0 = ไม่มีฐานเทียบ → ไม่ตัดสิน (text/note ตรงกัน)
                text = "ข้อมูลปริมาณไม่พอ (ไม่มีฐาน SMA20)"
                note = "vol_no_base"
            elif ok:
                text = f"ปริมาณซื้อขาย {ratio:.1f}× SMA20 (breakout แรง)"
                note = "vol>=1.5xSMA20"
            elif above:
                text = f"ปริมาณซื้อขาย {ratio:.1f}× SMA20 (เหนือค่าเฉลี่ยแต่ยังไม่ breakout)"
                note = "vol>SMA20"
            else:
                text = "ปริมาณซื้อขายต่ำกว่า SMA20"
                note = "vol<SMA20"
            notes.append(note)
            breakdown.append({"ok": ok, "text": text})

        # 4) RSI (ไม่สุดโต่ง)
        if rsi_last is not None:
            ok = rsi_last < 70 if is_buy else rsi_last > 30
            if ok:
                score += 1
            desc = _rsi_desc(rsi_last, is_buy)
            if ok:
                text = f"RSI = {rsi_last:.0f} ({desc})"
            else:
                warn = "overbought เสี่ยงไล่ของแพง" if is_buy else "oversold เสี่ยงรีบาวด์"
                text = f"RSI = {rsi_last:.0f} ({warn})"
            notes.append(f"RSI={rsi_last:.0f}")
            breakdown.append({"ok": ok, "text": text})

    high_quality = score >= 3 and trend_pass

    # Multi-timeframe: เช็กว่าสัญญาณตรงเทรนด์รายสัปดาห์ไหม (รันเฉพาะตอนมีสัญญาณ)
    mtf_aligned: Optional[bool] = None
    if enable_mtf and eval_is_buy is not None:
        try:
            wk_close = close.resample("W").last().dropna()
            # ตัดแท่งสัปดาห์ "ปัจจุบันที่ยังก่อตัว" ก่อนคิด EMA (no-repaint) — ใช้ได้ทั้งตลาด 5 วันและ 7 วัน
            # resample("W") ป้ายชื่อ bin = วันอาทิตย์ (W-SUN) · สัปดาห์ยัง "ก่อตัว" ถ้า "วันนี้ (UTC)"
            # ยังอยู่/ก่อนวันสิ้นสุด bin → ตัดทิ้ง · ถ้าผ่านวันอาทิตย์ไปแล้ว = ปิดสมบูรณ์ → เก็บไว้
            # *เทียบ "วันนี้" ไม่ใช่ index แท่งล่าสุด* — หุ้น/ทองแท่งล่าสุดเป็นศุกร์เสมอ (< อาทิตย์) ถ้าเทียบ
            #  index จะตัดสัปดาห์ที่ปิดครบแล้วทิ้งทุกครั้ง → weekly EMA ช้าไป 1 สัปดาห์ตลอด (bug ที่ review จับได้)
            if len(wk_close) >= 1:
                _today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
                _wend = pd.Timestamp(wk_close.index[-1])
                _wend = (_wend.tz_localize(None) if _wend.tzinfo is not None else _wend).normalize()
                if _today <= _wend:
                    wk_close = wk_close.iloc[:-1]
            if len(wk_close) >= ema_slow + 2:
                wk_fast = ema(wk_close, ema_fast)
                wk_slow = ema(wk_close, ema_slow)
                if not pd.isna(wk_fast.iloc[-1]) and not pd.isna(wk_slow.iloc[-1]):
                    wk_bull = bool(wk_fast.iloc[-1] > wk_slow.iloc[-1])
                    mtf_aligned = wk_bull if eval_is_buy else (not wk_bull)
        except Exception:  # noqa: BLE001 — MTF เป็นออปชัน พังก็ข้าม
            mtf_aligned = None

    if mtf_aligned is True:
        breakdown.append({"ok": True, "text": "ยืนยันด้วยกราฟรายสัปดาห์"})
    elif mtf_aligned is False:
        breakdown.append({"ok": False, "text": "ยังไม่ผ่านการยืนยันจากกราฟรายสัปดาห์"})

    # บังคับ bar_date เป็น tz-naive เสมอ — crypto (ccxt) คืน tz-aware UTC แต่
    # หุ้น/ทอง (yfinance) คืน tz-naive ถ้าไม่ strip จะเทียบ max() ข้ามกลุ่มไม่ได้
    last_ts = pd.Timestamp(df.index[-1])
    if last_ts.tzinfo is not None:
        last_ts = last_ts.tz_localize(None)

    stage = weinstein_stage(close)  # ภาพใหญ่เทรนด์ (None ถ้าแท่งไม่พอ ~170)

    return Signal(
        symbol=symbol,
        display_name=display_name or symbol,
        signal=sig,
        zone=zone_now,
        close=close_v,
        score=score,
        high_quality=high_quality,
        bar_date=last_ts.normalize(),
        notes=notes,
        mtf_aligned=mtf_aligned,
        breakdown=breakdown,
        atr=atr_last,
        adx=adx_last,
        rsi=rsi_last,
        vol_above=(vol_last > vol_avg_last) if (vol_last is not None and vol_avg_last is not None) else None,
        vol_ratio=(vol_last / vol_avg_last) if (vol_last is not None and vol_avg_last not in (None, 0)) else None,
        prev_zone=zone_prev,
        anticipate=eval_direction if sig == "none" else None,
        stage=stage,
        ema_fast=float(fast.iloc[-1]) if not pd.isna(fast.iloc[-1]) else None,
        ema_slow=float(slow.iloc[-1]) if not pd.isna(slow.iloc[-1]) else None,
        trend_q=trend_quality(close),
    )
