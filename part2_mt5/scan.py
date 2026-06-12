"""
part2_mt5/scan.py — Scalping Bot สแกนสัญลักษณ์ของ "โบรก (Exness)" เอง บนข้อมูล MT5 จริง

ใช้ตรรกะ CDC ของ Part 1 (core.signals.compute_signal) มาคำนวณ — reuse ไม่เขียนซ้ำ
*** สแกนเฉพาะสัญลักษณ์ที่โบรกมี เท่านั้น — ไม่ไปแตะสินทรัพย์นอกโบรก ***
"""
from __future__ import annotations
import logging
import os
import sys

# ให้ import core.* ของ Part 1 ได้ (Scalping Bot → ใช้ pure functions ของ Part 1, ทางเดียว)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

log = logging.getLogger("part2.scan")


def _is_sideway(sig, adx_max: float = 20.0) -> bool:
    """sideway แบบสมดุล (พอร์ตเดียวกับ Part 1): ADX ต่ำ + MA150 แบน + EMA พันกัน"""
    adx = sig.adx
    if adx is None or adx >= adx_max:
        return False
    st = sig.stage
    if st is not None and abs(st.get("slope_pct") or 0) >= 1.0:
        return False
    if sig.ema_fast and sig.ema_slow and sig.close:
        return abs(sig.ema_fast - sig.ema_slow) / sig.close < 0.005
    return False


def analyze(df, symbol: str) -> "dict | None":
    """รัน CDC บน OHLC ของ MT5 → bias dict (direction/zone/stage/...) หรือ None ถ้าไม่มีทิศ/ข้อมูลไม่พอ
    df = DataFrame จาก mt5_client.rates (มีคอลัมน์ time/open/high/low/close/volume)"""
    from core.signals import compute_signal
    import pandas as pd
    d = df.copy()
    if "time" in d.columns:
        d = d.set_index(pd.to_datetime(d["time"]))  # ต้องเป็น datetime index สำหรับ mtf/bar_date
    sig = compute_signal(d, symbol, enable_ema200_filter=True, min_bars_required=60,
                         enable_mtf=True, score_when_none=True)
    if sig is None:
        sig = compute_signal(d, symbol, enable_ema200_filter=False, min_bars_required=30,
                             enable_mtf=True, score_when_none=True)
    if sig is None:
        return None
    # ทิศ: สัญญาณจริง > โซน
    if sig.signal == "buy":
        direction = "buy"
    elif sig.signal == "sell":
        direction = "sell"
    elif sig.zone in ("green", "yellow"):
        direction = "buy"
    elif sig.zone in ("red", "lblue"):
        direction = "sell"
    else:
        return None  # orange/blue = สุดขั้ว (ย่อแรง/เด้งแรง) → ข้าม ยกเว้นมีสัญญาณ flip จริง
    st = sig.stage or {}
    tq = sig.trend_q or {}
    # ความ "คึกคัก" ของสินทรัพย์ — ใช้จัดอันดับ (เลือกตัววิ่งไว+วอลุ่มเยอะก่อน → ถึงเป้า % ไว)
    atr_pct = (sig.atr / sig.close * 100) if (sig.atr and sig.close) else 0.0
    vol_ratio = 1.0
    if "volume" in d.columns and len(d) >= 30:
        v = d["volume"].astype(float)
        avg = float(v.iloc[-30:].mean())
        if avg > 0:
            vol_ratio = float(v.iloc[-5:].mean() / avg)   # วอลุ่มล่าสุด เทียบค่าเฉลี่ย
    return {
        "symbol": symbol, "direction": direction, "zone": sig.zone, "signal": sig.signal,
        "is_fresh": sig.signal in ("buy", "sell"), "stars": sig.score, "high_quality": bool(sig.high_quality),
        "stage": st.get("n"), "stage_label": st.get("label"), "trend_r2": tq.get("r2"),
        "atr": sig.atr, "adx": sig.adx, "rsi": sig.rsi, "rs_rank": sig.rs_rank,
        "ema_fast": sig.ema_fast, "ema_slow": sig.ema_slow, "close": sig.close,
        "atr_pct": round(atr_pct, 3), "vol_ratio": round(vol_ratio, 2),
        "activity": round(atr_pct * max(vol_ratio, 0.5), 3),   # วิ่งไว × วอลุ่ม
        "sideway": _is_sideway(sig),
    }


def scan_broker(exsyms: list[str], mt5, cfg: dict) -> list[dict]:
    """สแกนสัญลักษณ์โบรก (D1) → list ของ bias ที่ 'มีทิศ + ไม่ sideway'
    เฉพาะตัวที่โบรกมีจริง (validate มาก่อนแล้วจาก caller)"""
    import MetaTrader5 as m5
    from datetime import datetime, timedelta
    trend_tf = cfg.get("TREND_TF", "D1")
    max_stale = int(cfg.get("MARKET_STALE_MIN", "180"))   # tick เก่ากว่านี้ = ตลาดปิด → ข้าม
    min_atr_pct = float(cfg.get("MIN_ATR_PCT", "0") or "0")  # ATR%/วัน ต่ำกว่านี้ = วิ่งช้า → ข้าม
    min_recent = float(cfg.get("MIN_RECENT_MOVE_PCT", "0") or "0")  # %ขยับขั้นต่ำช่วงล่าสุด (กันตัวนิ่งตอนนี้)
    recent_tf = cfg.get("RECENT_TF", "H1")
    recent_bars = int(cfg.get("RECENT_BARS", "12"))
    out: list[dict] = []
    for sym in exsyms:
        try:
            tick = m5.symbol_info_tick(sym)               # เช็ก tick ก่อน (เร็ว) — กันบล็อก mt5.rates
            if not tick or not tick.time:
                continue                                  # ไม่มี tick (ยังไม่อยู่ MarketWatch/ไม่มีข้อมูล) → ข้าม
            if (datetime.now() - datetime.fromtimestamp(tick.time)) > timedelta(minutes=max_stale):
                continue                                  # ตลาดปิด (tick เก่า) → ข้าม
            df = mt5.rates(sym, trend_tf, 320)
            if df is None or len(df) < 60:
                continue
            b = analyze(df, sym)
            if not b or not b["direction"]:
                continue
            if b["sideway"]:
                log.info("ข้าม %s — sideway", sym)
                continue
            if min_atr_pct > 0 and b.get("atr_pct", 0) < min_atr_pct:
                log.info("ข้าม %s — วิ่งช้า (ATR %.2f%%/วัน < %.2f%%)", sym, b.get("atr_pct", 0), min_atr_pct)
                continue
            if min_recent > 0:                            # กันตัวที่ "นิ่งตอนนี้" (เช่นคริปโตเสาร์-อาทิตย์)
                rdf = mt5.rates(sym, recent_tf, recent_bars + 5)
                if rdf is not None and len(rdf) >= recent_bars:
                    seg = rdf.iloc[-recent_bars:]
                    cl = float(seg["close"].iloc[-1])
                    rmove = (float(seg["high"].max()) - float(seg["low"].min())) / cl * 100 if cl > 0 else 0.0
                    b["recent_move_pct"] = round(rmove, 3)
                    if rmove < min_recent:
                        log.info("ข้าม %s — นิ่งตอนนี้ (ขยับ %.2f%% ใน %d แท่ง%s < %.2f%%)",
                                 sym, rmove, recent_bars, recent_tf, min_recent)
                        continue
            out.append(b)
        except Exception as e:  # noqa: BLE001
            log.warning("scan %s ล้มเหลว: %s", sym, e)
    # จัดอันดับ: ตัวคึกคัก (วิ่งไว + วอลุ่มเยอะ) ก่อน → เต็มสล็อตด้วยตัวที่ถึงเป้า % ไวสุด
    out.sort(key=lambda b: b.get("activity", 0), reverse=True)
    return out
