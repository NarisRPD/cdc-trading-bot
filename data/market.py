"""
data/market.py — ข้อมูลเสริมสำหรับ "คัด underlying แบบมือโปร option"

- hv_percentile(df): Historical Volatility 20 วัน + percentile ในปี → เบี้ยน่าจะแพง/ถูก
- earnings_days(ticker): จำนวนวันถึงงบรอบหน้า (yfinance) → เตือน IV crush
- atm_iv_snapshot(ticker, spot): IV/OI/spread ของ ATM จาก option chain (US เท่านั้น)
- options_context(...): รวมทุกอย่างเป็นข้อความเดียว ใช้ใน /scan SYMBOL + /callbuy /putbuy

ทั้งหมด best-effort — ดึงไม่ได้ก็คืน None/ข้อความว่าง ไม่ทำให้คำสั่งพัง
yfinance/option chain ใช้ได้จริงเฉพาะหุ้น US; ตลาดอื่นจะ fallback เป็น HV proxy
"""
from __future__ import annotations
import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

# ── cache สภาพคล่อง option (กัน yahoo ว่างช่วงรีเฟรชดึก US) ────────────────
_LIQ_CACHE_FILE = "option_liq_cache.json"
_LIQ_CACHE_TTL_DAYS = 5          # ใช้ค่าเก่าได้ไม่เกิน 5 วัน (OI ไม่เปลี่ยนเร็วมาก)
_LIQ_FIELDS = ("status", "oi", "spread_pct", "iv", "strike", "expiry")


def _liq_cache_get(ticker: str) -> Optional[dict]:
    """ค่าสภาพคล่องล่าสุด (good/poor) ของ ticker จาก cache — None ถ้าไม่มี/เก่าเกิน TTL"""
    try:
        from watchlist import store
        e = (store.load_json(_LIQ_CACHE_FILE, {}) or {}).get(ticker.upper())
        if not e or not e.get("ts"):
            return None
        if (pd.Timestamp.now(tz="UTC") - pd.Timestamp(e["ts"])).days > _LIQ_CACHE_TTL_DAYS:
            return None
        return e
    except Exception:  # noqa: BLE001
        return None


def liq_cache_put_many(mapping: dict) -> None:
    """เขียน cache เป็น batch (กัน race จากการ fetch ขนาน) — เก็บเฉพาะผล good/poor ที่สด (ไม่ใช่ cache)"""
    fresh = {tk.upper(): liq for tk, liq in (mapping or {}).items()
             if liq and liq.get("status") in ("good", "poor") and not liq.get("cached")}
    if not fresh:
        return
    try:
        from watchlist import store
        from datetime import datetime, timezone
        c = store.load_json(_LIQ_CACHE_FILE, {}) or {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for tk, liq in fresh.items():
            c[tk] = {k: liq.get(k) for k in _LIQ_FIELDS}
            c[tk]["ts"] = now
        store.save_json(_LIQ_CACHE_FILE, c)
        log.info("liq cache updated: %d tickers", len(fresh))
    except Exception as e:  # noqa: BLE001
        log.warning("liq cache put failed: %s", e)


def hv_percentile(df: Optional[pd.DataFrame]) -> Optional[dict]:
    """
    Historical Volatility (annualized, หน้าต่าง 20 วัน) + percentile เทียบ 1 ปีล่าสุด
    HV ต่ำ percentile ต่ำ = ราคาเงียบ → option (long) มักถูก · สูง = แพง
    คืน {"hv": 28.5, "pct": 35} หรือ None
    """
    try:
        import numpy as np
        if df is None or df.empty:
            return None
        close = df["close"].astype(float).dropna()
        if len(close) < 40:
            return None
        logret = np.log(close / close.shift(1)).dropna()
        hv = logret.rolling(20).std() * np.sqrt(252)
        hv = hv.dropna()
        if len(hv) < 30:
            return None
        cur = float(hv.iloc[-1])
        window = hv.iloc[-252:] if len(hv) >= 252 else hv
        pct = round(100.0 * float((window < cur).sum()) / len(window))
        return {"hv": round(cur * 100, 1), "pct": int(pct)}
    except Exception as e:  # noqa: BLE001
        log.warning("hv_percentile failed: %s", e)
        return None


def earnings_days(ticker: str) -> Optional[dict]:
    """
    จำนวนวันถึงวันประกาศงบรอบถัดไป (yfinance) — ใช้เตือน IV crush
    คืน {"days": 7, "date": "12/06"} หรือ None (ไม่มีข้อมูล/ไม่ใช่หุ้นที่มีงบ)
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        ed = t.get_earnings_dates(limit=12)
        if ed is None or ed.empty:
            return None
        now = pd.Timestamp.now(tz="UTC")
        future = [pd.Timestamp(d) for d in ed.index if pd.Timestamp(d) >= now]
        if not future:
            return None
        nxt = min(future)
        days = (nxt - now).days
        return {"days": int(days), "date": nxt.strftime("%d/%m")}
    except Exception as e:  # noqa: BLE001
        log.warning("earnings_days failed for %s: %s", ticker, e)
        return None


def atm_iv_snapshot(ticker: str, spot: Optional[float], *, target_dte: Optional[int] = None) -> Optional[dict]:
    """
    Implied Volatility + OI + bid/ask spread ของ ATM (option chain — US เท่านั้น)
    target_dte = DTE ที่แนะนำ (จากบรรทัด ⏱️) → เลือก expiry ใกล้สัญญาที่จะซื้อจริง
    ถ้าไม่ให้ → ใช้ monthly ~40 วันเป็น default. คืน dict หรือ None
    """
    try:
        import yfinance as yf
        if spot is None or spot <= 0:
            return None
        t = yf.Ticker(ticker)
        exps = list(getattr(t, "options", []) or [])
        if not exps:
            return {"no_options": True}  # หุ้นนี้ไม่มี option listed (ต่างจากดึงไม่ได้ = None)
        now = pd.Timestamp.now()
        fut = [(e, (pd.Timestamp(e) - now).days) for e in exps]
        fut = [(e, d) for e, d in fut if d >= 7]  # อย่างน้อย 7 วัน (เลี่ยงของใกล้หมดอายุ)
        if not fut:
            target = exps[-1]
        elif target_dte:
            # เลือก expiry ใกล้ DTE ที่ "แนะนำ" (จาก ⏱️) — เน้น >= target ก่อน แล้วใกล้สุด
            # = เช็กสภาพคล่องของสัญญาที่จะซื้อจริง (ไม่ fixed weekly/monthly)
            target = min(fut, key=lambda x: (0 if x[1] >= target_dte else 1, abs(x[1] - target_dte)))[0]
        else:
            # ไม่มี DTE แนะนำ (เช่น spot) → ใช้ monthly ~40 วัน (OI หนา) เป็น default
            monthly = [(e, d) for e, d in fut
                       if 25 <= d <= 70 and pd.Timestamp(e).weekday() == 4 and 15 <= pd.Timestamp(e).day <= 21]
            target = (min(monthly, key=lambda x: abs(x[1] - 40)) if monthly
                      else min(fut, key=lambda x: abs(x[1] - 40)))[0]

        chain = t.option_chain(target)
        calls, puts = chain.calls, chain.puts
        if calls is None or calls.empty:
            return None
        calls = calls.copy()
        calls["_dist"] = (calls["strike"].astype(float) - spot).abs()
        row = calls.sort_values("_dist").iloc[0]  # ATM call → IV + spread

        iv_raw = row.get("impliedVolatility")
        iv = float(iv_raw) if iv_raw == iv_raw and iv_raw else None  # กัน NaN
        bid = float(row.get("bid") or 0)
        ask = float(row.get("ask") or 0)
        spread_pct = round(100.0 * (ask - bid) / ((ask + bid) / 2)) if (ask > 0 and bid > 0) else None
        oi_raw = row.get("openInterest")
        atm_oi = int(oi_raw) if (oi_raw == oi_raw and oi_raw is not None) else 0

        # OI รวมใกล้ ATM (±15% ของ spot) ทั้ง call + put — robust กว่าใช้ strike เดียว
        # (กัน yahoo ส่ง OI หาย/0 บาง strike จนเข้าใจผิดว่าหุ้น mega-cap ไม่คล่อง)
        lo, hi = spot * 0.85, spot * 1.15

        def _sum_oi(d) -> int:
            try:
                if d is None or d.empty or "openInterest" not in d.columns:
                    return 0
                k = d["strike"].astype(float)
                return int(d.loc[(k >= lo) & (k <= hi), "openInterest"].fillna(0).sum())
            except Exception:  # noqa: BLE001
                return 0

        near_oi = _sum_oi(calls) + _sum_oi(puts)

        snap = {
            "iv": round(iv * 100, 1) if iv else None,
            "oi": atm_oi,
            "near_oi": near_oi,
            "spread_pct": spread_pct,
            "expiry": target,
            "strike": float(row["strike"]),
        }
        chosen_dte = (pd.Timestamp(target) - now).days
        log.info("optliq %s: target_dte=%s exp=%s(%dd) strike=%.1f atm_oi=%s near_oi=%s spread=%s iv=%s",
                 ticker, target_dte, target, chosen_dte, snap["strike"], atm_oi, near_oi, spread_pct, snap["iv"])
        return snap
    except Exception as e:  # noqa: BLE001
        log.warning("atm_iv_snapshot failed for %s: %s", ticker, e)
        return None


def option_liquidity(
    ticker: str,
    spot: Optional[float],
    *,
    target_dte: Optional[int] = None,
    min_oi: int = 500,
    max_spread_pct: float = 40.0,
) -> dict:
    """
    ประเมินสภาพคล่อง option (US เท่านั้น) ของ expiry ที่ "แนะนำ" (target_dte จาก ⏱️)
    good = OI สูงพอ + spread แคบ · poor = ไม่ผ่านเกณฑ์ · unknown = ดึงไม่ได้ (ไม่กรอง)
    """
    snap = atm_iv_snapshot(ticker, spot, target_dte=target_dte)
    if snap and snap.get("no_options"):
        return {"status": "none"}  # หุ้นนี้ไม่มี option ให้เทรด
    # expiry ที่แนะนำไม่มี OI (ไกลไป/ยังไม่สะสม เช่น CVX 78 วัน) → ลอง monthly เพื่ออ่านจริง
    if target_dte and (not snap or (snap.get("near_oi") or 0) <= 0):
        alt = atm_iv_snapshot(ticker, spot, target_dte=None)
        if alt and (alt.get("near_oi") or 0) > 0:
            snap = alt
    if not snap:
        return {"status": "unknown", "reason": "fetch"}  # ดึง chain ไม่ได้จริง
    near_oi = snap.get("near_oi") or 0
    sp = snap.get("spread_pct")
    base = {
        "oi": near_oi,
        "spread_pct": sp,
        "iv": snap.get("iv"),
        "strike": snap.get("strike"),
        "expiry": snap.get("expiry"),
    }
    # chain มา แต่ค่า OI/IV/bid-ask = 0 ทั้งหมด → yahoo รีเฟรชนอกเวลาตลาด US (ว่างชั่วคราว)
    # → ลองดึงค่าล่าสุดที่ "ดี" จาก cache มาใช้ก่อน (ไม่งั้น unknown)
    if near_oi <= 0:
        cached = _liq_cache_get(ticker)
        if cached:
            return {**{k: cached.get(k) for k in _LIQ_FIELDS},
                    "cached": True, "cached_ts": cached.get("ts")}
        return {"status": "unknown", "reason": "empty", **base}
    good = near_oi >= min_oi and (sp is None or sp <= max_spread_pct)
    return {"status": "good" if good else "poor", **base}


def options_context(
    market: str,
    ticker: str,
    *,
    df: Optional[pd.DataFrame] = None,
    spot: Optional[float] = None,
    liq: Optional[dict] = None,
    crypto_exchange: str = "binance",
) -> str:
    """
    บล็อก "ข้อมูลเชิง option" — IV vs HV (แพง/ถูก) + สภาพคล่อง + เตือนงบ/IV crush
    market=="us" ได้ครบ (IV จริง) · ตลาดอื่นใช้ HV proxy · crypto ไม่ทำ (ไม่มี option)
    liq = ผล option_liquidity() ที่ดึงไว้แล้ว (กัน fetch option chain ซ้ำ)
    """
    if market == "crypto":
        return ""
    try:
        if df is None:
            from data.quote import fetch_history
            df = fetch_history(market, ticker, crypto_exchange=crypto_exchange)
        if spot is None and df is not None and not df.empty:
            spot = float(df["close"].iloc[-1])
    except Exception:  # noqa: BLE001
        df = df

    lines: list[str] = []
    hv = hv_percentile(df)
    # ใช้ liquidity ที่ดึงไว้แล้วถ้ามี (มี iv/oi/spread เหมือนกัน) ไม่งั้นค่อยดึง option chain
    iv_snap = liq if liq is not None else (atm_iv_snapshot(ticker, spot) if market == "us" else None)

    if iv_snap and iv_snap.get("iv") is not None:
        iv = iv_snap["iv"]
        line = f"📊 IV (ATM ~{iv_snap['expiry']}) = {iv:.0f}%"
        if hv and hv["hv"] > 0:
            ratio = iv / hv["hv"]
            tag = ("แพง — ระวังจ่าย premium แพง" if ratio >= 1.2
                   else "ถูก — เบี้ยน่าสนใจ" if ratio <= 0.9 else "สมเหตุผล")
            line += f" · HV20 = {hv['hv']:.0f}% → option {tag}"
        lines.append(line)
        oi = iv_snap.get("oi") or 0
        sp = iv_snap.get("spread_pct")
        liq = ("✅ สภาพคล่องดี" if (oi >= 500 and (sp is None or sp <= 10))
               else "⚠️ สภาพคล่องบาง (OI ต่ำ/spread กว้าง — ระวัง slippage)")
        sp_txt = f", spread ~{sp}%" if sp is not None else ""
        lines.append(f"   OI {oi:,}{sp_txt} — {liq}")
    elif hv:
        verdict = ("ต่ำ → เบี้ย option น่าจะถูก (ดีต่อการซื้อ Call/Put)" if hv["pct"] <= 35
                   else "สูง → เบี้ย option น่าจะแพง (ระวัง)" if hv["pct"] >= 65 else "ปานกลาง")
        lines.append(f"📉 ความผันผวน HV20 = {hv['hv']:.0f}% (percentile {hv['pct']} ของปี) → {verdict}")

    if market in ("us", "thai"):
        earn = earnings_days(ticker)
        if earn:
            d = earn["days"]
            if d <= 14:
                lines.append(f"⚠️ งบออกอีก {d} วัน ({earn['date']}) — เสี่ยง IV crush! "
                             "ระวังซื้อ option ตอนนี้ (เบี้ยพอง พองบยุบ)")
            else:
                lines.append(f"📅 งบรอบหน้า ~{earn['date']} (อีก {d} วัน)")

    return "\n".join(lines)


def option_note_for_position(
    market: str,
    ticker: str,
    side: str,
    *,
    df: Optional[pd.DataFrame] = None,
) -> str:
    """
    บรรทัดกระชับสำหรับ /list — เตือนงบ (หุ้น US/ไทย) + เบี้ยถูก/แพงจาก HV (option)
    เร็ว: ใช้ df ที่ full_status ดึงมาแล้ว (ไม่ fetch ซ้ำ) + ไม่แตะ option chain (IV จริงดูใน /scan)
    คืน "" ถ้าไม่มีอะไรน่าเตือน (crypto/commodity/ดึงไม่ได้)
    """
    if market not in ("us", "thai"):
        return ""
    bits: list[str] = []
    earn = earnings_days(ticker)  # 1 yfinance call — เบาพอ
    if earn and earn["days"] <= 21:
        icon = "⚠️" if earn["days"] <= 7 else "📅"
        crush = " เสี่ยง IV crush" if side in ("call", "put") and earn["days"] <= 7 else ""
        bits.append(f"{icon} งบ {earn['date']} (อีก {earn['days']}d){crush}")
    # เบี้ยถูก/แพงจาก HV percentile — เฉพาะ option (spot ถือหุ้นจริง ไม่มี premium decay)
    if side in ("call", "put"):
        hv = hv_percentile(df)
        if hv:
            tag = "ถูก" if hv["pct"] <= 35 else "แพง" if hv["pct"] >= 65 else "พอดี"
            bits.append(f"HV {hv['hv']:.0f}% (เบี้ยน่าจะ{tag})")
    return " · ".join(bits)
