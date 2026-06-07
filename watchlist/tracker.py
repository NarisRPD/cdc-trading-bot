"""
watchlist/tracker.py — business logic ของ watchlist (แยกจาก I/O)

- make_position(): สร้าง position พร้อม snapshot โซน/ราคาตอนเข้า
- quick_status(): ราคาปัจจุบัน + %P/L (เร็ว ใช้ใน /list)
- full_status(): + โซน CDC ปัจจุบัน + ธงเตือนปิด (ใช้ใน scanner รายวัน)

กำไร/ขาดทุน:
- spot / call → (ปัจจุบัน - เข้า) / เข้า   (กำไรเมื่อราคาขึ้น)
- put         → (เข้า - ปัจจุบัน) / เข้า   (กำไรเมื่อราคาลง)
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.indicators import atr, adx as _adx
from core.signals import compute_signal
from core.symbols import Resolved
from data.quote import fetch_history, last_price

# side ที่กำไรเมื่อราคาขึ้น (spot/call) — ที่เหลือ (put) กำไรเมื่อราคาลง
_LONGISH = {"spot", "call"}


def _atr_mult() -> float:
    """ตัวคูณ ATR สำหรับ Stop Loss (default 2.0) — ปรับผ่าน env ATR_MULT"""
    try:
        return float(os.getenv("ATR_MULT", "2.0"))
    except ValueError:
        return 2.0


def _risk_mode() -> str:
    """safe = SL อิงโครงสร้าง (swing) + พื้น ATR | standard = ATR ล้วน"""
    return os.getenv("RISK_MODE", "safe").strip().lower()


def swing_lookback() -> int:
    try:
        return int(os.getenv("SWING_LOOKBACK", "10"))
    except ValueError:
        return 10


def compute_risk_levels(
    side: str,
    entry: float,
    atr_val: float,
    *,
    swing_low: Optional[float] = None,
    swing_high: Optional[float] = None,
    mode: Optional[str] = None,
) -> Optional[dict]:
    """
    คำนวณ SL/TP — 2 โหมด:
    - safe (default): SL อิง swing ± 0.5ATR แต่ "ไกลอย่างน้อย 2×ATR" (กัน noise) → ปลอดภัยสุด
    - standard: SL = ราคาเข้า ∓ (ATR_MULT × ATR)
    TP ทั้งสองโหมดที่ R:R 1:1 / 2:1 / 3:1 (R = ระยะ SL)
    คืน None ถ้าข้อมูลไม่พอ
    """
    if entry is None or atr_val is None or atr_val <= 0 or entry <= 0:
        return None
    mode = (mode or _risk_mode())
    floor = _atr_mult() * atr_val  # พื้นความปลอดภัยขั้นต่ำ

    if side in _LONGISH:
        sl = entry - floor
        if mode == "safe" and swing_low is not None:
            sl = min(swing_low - 0.5 * atr_val, sl)  # ไกลกว่า = ปลอดภัยกว่า
        risk = entry - sl
        if risk <= 0:  # guard กรณีผิดปกติ → กลับไปใช้ ATR floor
            risk = floor
            sl = entry - risk
        tp1, tp2, tp3 = entry + risk, entry + 2 * risk, entry + 3 * risk
    else:  # put — กลับด้าน
        sl = entry + floor
        if mode == "safe" and swing_high is not None:
            sl = max(swing_high + 0.5 * atr_val, sl)
        risk = sl - entry
        if risk <= 0:
            risk = floor
            sl = entry + risk
        tp1, tp2, tp3 = entry - risk, entry - 2 * risk, entry - 3 * risk

    return {
        "sl": round(sl, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "tp3": round(tp3, 6),
        "atr": round(atr_val, 6),
        "risk_pct": round(risk / entry * 100.0, 2),  # % ขาดทุนถ้าโดน SL
        "risk_mode": mode,
    }


def _fmt(v: float) -> str:
    if v is None:
        return "—"
    return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 100 else f"{v:,.2f}"


def format_risk_levels(pos: dict) -> str:
    """ข้อความ TP/SL เต็ม (ใช้ตอนตอบ /buy)"""
    if not pos.get("sl"):
        return ""
    safe = pos.get("risk_mode", "safe") == "safe"
    head = "📐 แนะนำ (โหมดปลอดภัย: swing + ATR)" if safe else f"📐 แนะนำ (ATR×{_atr_mult():g})"
    plan = (
        "\n💡 แผนปลอดภัย: ถึง TP1 → ปิดบางส่วน + เลื่อน SL มาที่ราคาทุน (breakeven)\n"
        "   พอทำแล้วไม้นี้ไม่เสี่ยงต่อ; ส่วนที่เหลือใช้ trailing ตามเทรนด์"
    ) if safe else ""
    return (
        f"\n\n{head}:\n"
        f"🛑 SL: {_fmt(pos['sl'])}  (เสี่ยง ~{pos.get('risk_pct','?')}%)\n"
        f"🎯 TP1 (1:1): {_fmt(pos['tp1'])}\n"
        f"🎯 TP2 (2:1): {_fmt(pos['tp2'])} ⭐\n"
        f"🎯 TP3 (3:1): {_fmt(pos['tp3'])}"
        f"{plan}\n"
        f"※ คำแนะนำเชิงกลไก ไม่ใช่คำแนะนำลงทุน — ปรับตามแผน/ขนาดไม้ของคุณ"
    )


def recommended_min_dte(atr_val: Optional[float], adx_val: Optional[float]) -> Optional[int]:
    """
    DTE ขั้นต่ำที่แนะนำ (ตรงกับเลขในบรรทัด ⏱️) — ใช้เลือก expiry ตอนเช็กสภาพคล่อง
    เพื่อให้ "เช็กสัญญาที่จะซื้อจริง" ไม่ใช่ expiry มั่ว ๆ. คืน None ถ้าไม่มี ATR
    """
    if not atr_val or atr_val <= 0:
        return None
    eff = max(0.15, min(0.45, (adx_val / 100.0) if adx_val else 0.25))
    far_cal = max(2, round((3.0 / eff) * 1.4))
    return min(120, max(21, round(far_cal * 1.8)))


def time_to_target_hint(atr_val: Optional[float], adx_val: Optional[float], *, full: bool = False) -> str:
    """
    คาดคะเนเวลาที่ราคาจะถึงเป้า แล้วแนะนำวันหมดอายุ Option ขั้นต่ำ (รายตัว ต่างกันตามสัญญาณ)
    - เป้าอิง ATR (ใกล้ 1.5×ATR / ไกล 3×ATR)
    - ความเร็ว = สัดส่วน ATR ที่ราคาเดินสุทธิต่อวัน ผันตาม ADX (เทรนด์แรง = เร็ว)
    - หมดอายุแนะนำ = เวลาถึงเป้าไกล × ~1.8 (เผื่อเวลา + กัน theta), ขั้นต่ำ 21 / สูงสุด 120 วัน
    """
    if not atr_val or atr_val <= 0:
        return ""
    eff = (adx_val / 100.0) if adx_val else 0.25          # net ATR fraction ต่อวัน
    eff = max(0.15, min(0.45, eff))
    near_cal = max(1, round((1.5 / eff) * 1.4))           # วันปฏิทิน (5 เทรด ≈ 7 วัน)
    far_cal = max(2, round((3.0 / eff) * 1.4))
    min_dte = recommended_min_dte(atr_val, adx_val)
    exp = (datetime.now() + timedelta(days=min_dte)).strftime("%d/%m")
    line = f"⏱️ คาดถึงเป้า ~{near_cal}-{far_cal} วัน → Option หมดอายุ ≥ {exp} (~{min_dte} วัน)"
    if full:
        line += "\n   (ปิด/โรลก่อนหมดอายุ ~2 สัปดาห์ ตอน theta เร่ง)"
    return line


def _trail_enabled() -> bool:
    return os.getenv("TRAIL_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y", "on")


def apply_trailing(pos: dict, current_price: Optional[float]) -> Optional[float]:
    """
    Trailing stop แบบขั้น R: กำไร +1R → SL มาทุน, +2R → SL มา +1R, +3R → +2R ...
    คืน SL ใหม่ถ้าควรเลื่อน (แน่นขึ้นเท่านั้น ไม่ถอยกลับ) มิฉะนั้น None
    R = ระยะ entry→TP1 (= ระยะ SL เดิม) ซึ่งคงที่แม้ SL จะถูกเลื่อนไปแล้ว
    """
    if not _trail_enabled() or current_price is None:
        return None
    entry = pos.get("entry_price")
    tp1 = pos.get("tp1")
    sl = pos.get("sl")
    if entry is None or tp1 is None or sl is None:
        return None
    R = abs(tp1 - entry)
    if R <= 0:
        return None

    if pos["side"] in _LONGISH:
        profit_r = (current_price - entry) / R
        if profit_r < 1:
            return None
        new_sl = entry + (int(profit_r) - 1) * R     # +1R→entry, +2R→entry+1R ...
        return round(new_sl, 6) if new_sl > sl + 1e-9 else None  # เลื่อนขึ้นเท่านั้น
    else:  # put
        profit_r = (entry - current_price) / R
        if profit_r < 1:
            return None
        new_sl = entry - (int(profit_r) - 1) * R
        return round(new_sl, 6) if new_sl < sl - 1e-9 else None  # เลื่อนลงเท่านั้น


def _pnl_pct(side: str, entry: float, current: float) -> float:
    if entry == 0:
        return 0.0
    if side in _LONGISH:
        return (current - entry) / entry * 100.0
    return (entry - current) / entry * 100.0


def format_option_thesis(pos: dict) -> str:
    """
    บล็อก thesis สำหรับ Call/Put — เป้า/จุด thesis เสีย บน "หุ้นอ้างอิง"
    (ไม่ใช่ราคา premium ของ option) ใช้ระดับ SL/TP ที่คำนวณไว้แล้วแต่ relabel
    """
    sl = pos.get("sl")
    tp1 = pos.get("tp1")
    tp2 = pos.get("tp2")
    entry = pos.get("entry_price")
    if sl is None or tp1 is None or tp2 is None or not entry:
        return ""
    is_call = pos["side"] == "call"
    dir_word = "ขึ้น" if is_call else "ลง"
    opp_word = "ลง" if is_call else "ขึ้น"
    opt = "Call" if is_call else "Put"

    def pct(p: float) -> str:
        return f"{(p - entry) / entry * 100:+.1f}%"

    return (
        f"📊 thesis หุ้นอ้างอิง ({opt} กำไรเมื่อหุ้น{dir_word}):\n"
        f"🎯 เป้าหุ้น{dir_word}: {_fmt(tp1)} ({pct(tp1)}) → {_fmt(tp2)} ⭐ ({pct(tp2)})\n"
        f"🛑 ปิด {opt} ถ้าหุ้น{opp_word}ทะลุ {_fmt(sl)} ({pct(sl)}) = thesis เสีย"
    )


def make_position(
    resolved: Resolved,
    side: str,
    entry_price: Optional[float] = None,
    *,
    strike: Optional[float] = None,
    crypto_exchange: str = "binance",
) -> dict:
    """
    สร้าง position dict — ดึงข้อมูล 1 ครั้งเพื่อ snapshot โซน + ราคา
    ถ้าผู้ใช้ไม่ใส่ราคาเข้า → ใช้ราคาปิดล่าสุดเป็นราคาอ้างอิง
    """
    zone: Optional[str] = None
    last_close: Optional[float] = None
    atr_val: Optional[float] = None
    adx_val: Optional[float] = None
    swing_low: Optional[float] = None
    swing_high: Optional[float] = None

    df = fetch_history(resolved.market, resolved.data_ticker, crypto_exchange=crypto_exchange)
    if df is not None and not df.empty:
        # ปิด ema200 filter เพื่อให้คำนวณโซนได้แม้ข้อมูล < 200 แท่ง (โซนใช้แค่ EMA12/26)
        sig = compute_signal(
            df, resolved.data_ticker,
            enable_ema200_filter=False, min_bars_required=30,
        )
        if sig is not None:
            zone = sig.zone
        last_close = float(df["close"].iloc[-1])
        atr_series = atr(df["high"], df["low"], df["close"], 14)
        if not atr_series.empty and atr_series.iloc[-1] == atr_series.iloc[-1]:  # not NaN
            atr_val = float(atr_series.iloc[-1])
        adx_series = _adx(df["high"], df["low"], df["close"], 14)
        if not adx_series.empty and adx_series.iloc[-1] == adx_series.iloc[-1]:
            adx_val = float(adx_series.iloc[-1])
        lb = swing_lookback()
        swing_low = float(df["low"].tail(lb).min())
        swing_high = float(df["high"].tail(lb).max())

    entry = entry_price if entry_price is not None else last_close

    pos = {
        "symbol": resolved.data_ticker,
        "display": resolved.display,
        "market": resolved.market,
        "side": side,
        "entry_price": entry,
        "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entry_zone": zone,
        "adx": adx_val,
        "strike": strike,
    }
    levels = compute_risk_levels(
        side, entry, atr_val, swing_low=swing_low, swing_high=swing_high,
    )
    if levels:
        pos.update(levels)  # sl, tp1, tp2, tp3, atr, risk_pct, risk_mode
    return pos


def quick_status(pos: dict, *, crypto_exchange: str = "binance") -> dict:
    """ราคาปัจจุบัน + %P/L (เร็ว — ไม่คำนวณโซน) สำหรับ /list"""
    cur = last_price(pos["market"], pos["symbol"], crypto_exchange=crypto_exchange)
    pnl = None
    if cur is not None and pos.get("entry_price"):
        pnl = _pnl_pct(pos["side"], float(pos["entry_price"]), cur)
    return {"current_price": cur, "pnl_pct": pnl}


def full_status(pos: dict, *, crypto_exchange: str = "binance") -> dict:
    """ราคา + %P/L + โซน CDC ปัจจุบัน + ธงเตือนปิด (ใช้ใน scanner รายวัน)"""
    cur: Optional[float] = None
    zone: Optional[str] = None
    stage: Optional[dict] = None
    exit_alert = False

    df = fetch_history(pos["market"], pos["symbol"], crypto_exchange=crypto_exchange)
    if df is not None and not df.empty:
        sig = compute_signal(
            df, pos["symbol"], enable_ema200_filter=False, min_bars_required=30,
        )
        if sig is not None:
            zone = sig.zone
            stage = sig.stage  # Weinstein Stage (ภาพใหญ่เทรนด์)
            # เตือนแบบ persistent (เตือนทุกวันที่ยังอยู่โซนสวนทาง ไม่ใช่แค่วันแรก
            # — กันพลาด report) : ถือ spot/call แต่เข้าโซนขาย (red) / ถือ put แต่เข้าโซนซื้อ (green)
            if pos["side"] in _LONGISH and zone == "red":
                exit_alert = True
            elif pos["side"] == "put" and zone == "green":
                exit_alert = True
        cur = float(df["close"].iloc[-1])

    if cur is None:
        cur = last_price(pos["market"], pos["symbol"], crypto_exchange=crypto_exchange)

    pnl = None
    if cur is not None and pos.get("entry_price"):
        pnl = _pnl_pct(pos["side"], float(pos["entry_price"]), cur)

    # เช็กว่าราคาปัจจุบันแตะ SL หรือ TP ระดับไหน
    sl_hit = False
    tp_level = 0
    if cur is not None and pos.get("sl") is not None:
        if pos["side"] in _LONGISH:
            sl_hit = cur <= pos["sl"]
            for i, key in enumerate(("tp1", "tp2", "tp3"), start=1):
                if pos.get(key) is not None and cur >= pos[key]:
                    tp_level = i
        else:  # put
            sl_hit = cur >= pos["sl"]
            for i, key in enumerate(("tp1", "tp2", "tp3"), start=1):
                if pos.get(key) is not None and cur <= pos[key]:
                    tp_level = i

    return {
        "current_price": cur,
        "current_zone": zone,
        "stage": stage,  # Weinstein Stage 1-4 (None ถ้าแท่งไม่พอ)
        "pnl_pct": pnl,
        "exit_alert": exit_alert,
        "sl_hit": sl_hit,
        "tp_level": tp_level,
        "df": df,  # ส่ง OHLCV ที่ดึงแล้วกลับไป — /list เอาไปคำนวณ HV ได้โดยไม่ fetch ซ้ำ
    }
