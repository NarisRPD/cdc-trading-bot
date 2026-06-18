"""
signals_log.py — บันทึก + ประเมินผลสัญญาณ CDC แบบ mechanical (forward-test)

เป้าหมาย: "วัดความแม่นของสัญญาณด้วยหลักฐาน" โดยไม่พึ่งการเข้า/ปิดของผู้ใช้
(journal เดิมสะท้อนวินัยผู้ใช้ = selection bias — ตัวนี้วัด edge ของสัญญาณตรง ๆ)

flow:
  log_signals()       — ทุกสแกน append สัญญาณ buy/sell ที่ "โชว์จริง" (idempotent: symbol|bar_date|dir)
                        แนบ score/HQ/mtf/rs/zone/stage/regime_bull (D4) ไว้คาลิเบรตทีหลัง
  evaluate_outcomes() — รันรายวัน: สัญญาณที่ครบอายุ → ดึง OHLCV ไปข้างหน้า → label
                        win  = แตะ +TARGET_ATR×ATR ก่อน
                        loss = แตะ -STOP_ATR×ATR ก่อน (หรือแตะทั้งคู่ในแท่งเดียว = อนุรักษ์)
                        expired = ครบ LOOKAHEAD แท่งไม่ถึงเป้า/จุดตัด
  calib_summary()     — group ตาม ดาว/HQ/regime/ทิศ → win-rate เชิงประจักษ์ (ป้อน /calib · D2)

persistence: GCS ผ่าน watchlist.store (Cloud Run filesystem ลบทุกรอบ) — local fallback ตอนเทส
ทุกฟังก์ชัน best-effort: พังก็ไม่ทำสแกนล่ม
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_LOG_FILE = "signals_log.json"
_MAX_ENTRIES = 8000   # prune กันไฟล์บวม
_MARKET_OF = {"Crypto": "crypto", "US Stocks": "us", "Thai Stocks": "thai", "Commodities": "commodity"}
_REGIME_REF = {"crypto": ("crypto", "BTC/USDT"), "us": ("us", "^GSPC"),
               "thai": ("thai", "^SET.BK"), "commodity": ("commodity", "GC=F")}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _int_env(name: str, default: int) -> int:
    """อ่าน env เป็น int — ค่าพังให้ default + log (อย่าเงียบ ๆ ทำทั้ง eval ล่ม)"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        log.warning("env %s ค่าไม่ใช่ int — ใช้ default %d", name, default)
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        log.warning("env %s ค่าไม่ใช่ float — ใช้ default %s", name, default)
        return default


def _regime_bull(market: str, cfg) -> Optional[bool]:
    """ดัชนีอ้างอิงของตลาดอยู่เหนือ EMA200 ไหม (bull) — None ถ้าดึงไม่ได้ (D4)"""
    ref = _REGIME_REF.get(market)
    if not ref:
        return None
    try:
        from data.quote import fetch_history
        from core.indicators import ema
        df = fetch_history(ref[0], ref[1], crypto_exchange=cfg.crypto_exchange)
        if df is None or len(df) < 200:
            return None
        c = df["close"].astype(float)
        e200 = float(ema(c, 200).iloc[-1])
        if e200 != e200:  # NaN
            return None
        return bool(float(c.iloc[-1]) > e200)
    except Exception as e:  # noqa: BLE001
        log.warning("regime_bull(%s) failed: %s", market, e)
        return None


def log_signals(results, cfg) -> int:
    """append สัญญาณ buy/sell ที่โชว์จริงลง GCS log (idempotent) + ฟีเจอร์ setup + regime"""
    try:
        from watchlist import store
        from main import _filter_for_alert
    except Exception as e:  # noqa: BLE001
        log.warning("log_signals import ไม่ได้: %s", e)
        return 0
    try:
        rows = store.load_json(_LOG_FILE, [])
        if not isinstance(rows, list):
            rows = []
        seen = {r.get("id") for r in rows}
        now = _now_iso()
        regime_cache: dict = {}
        added = 0
        for r in results:
            mk = _MARKET_OF.get(r.group_name, "")
            if mk not in regime_cache:
                regime_cache[mk] = _regime_bull(mk, cfg)
            bull = regime_cache[mk]
            for direction, sigs in (("buy", _filter_for_alert(r.buy, cfg)),
                                    ("sell", _filter_for_alert(r.sell, cfg))):
                for s in sigs:
                    bd = s.bar_date.strftime("%Y-%m-%d") if s.bar_date is not None else None
                    if bd is None:
                        continue
                    eid = f"{s.symbol}|{bd}|{direction}"
                    if eid in seen:
                        continue
                    seen.add(eid)
                    rows.append({
                        "id": eid, "logged_at": now,
                        "symbol": s.symbol, "market": mk, "direction": direction,
                        "bar_date": bd, "close": s.close, "atr": s.atr,
                        "score": s.score, "high_quality": bool(s.high_quality),
                        "mtf_aligned": s.mtf_aligned, "rs_rank": s.rs_rank,
                        "zone": s.zone, "prev_zone": s.prev_zone,
                        "stage": (s.stage or {}).get("n"), "adx": s.adx, "rsi": s.rsi,
                        "vol_ratio": s.vol_ratio, "regime_bull": bull,
                        "outcome": None, "evaluated_at": None,
                    })
                    added += 1
        if len(rows) > _MAX_ENTRIES:
            rows = rows[-_MAX_ENTRIES:]
        store.save_json(_LOG_FILE, rows)
        log.info("signals_log: +%d สัญญาณ (รวม %d)", added, len(rows))
        return added
    except Exception as e:  # noqa: BLE001
        log.warning("log_signals ล้มเหลว: %s", e)
        return 0


def evaluate_outcomes(cfg, *, max_eval: int = 80) -> int:
    """forward-test สัญญาณที่ครบอายุ → label win/loss/expired. คืนจำนวนที่เพิ่งได้ผล"""
    try:
        from watchlist import store
        from data.quote import fetch_history
        import pandas as pd
        from collections import defaultdict
    except Exception as e:  # noqa: BLE001
        log.warning("evaluate_outcomes import ไม่ได้: %s", e)
        return 0
    target_k = _float_env("EVAL_TARGET_ATR", 1.5)
    stop_k = _float_env("EVAL_STOP_ATR", 1.5)
    lookahead = _int_env("EVAL_LOOKAHEAD_BARS", 10)
    try:
        rows = store.load_json(_LOG_FILE, [])
        if not isinstance(rows, list) or not rows:
            return 0
        today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
        # ครบอายุ = ผ่าน lookahead แท่งทำการแล้ว (เผื่อวันหยุด ~×1.6 วันปฏิทิน)
        min_age = max(lookahead, int(lookahead * 1.6))
        # ปิดผลเป็น expired ได้เฉพาะเมื่อเก็บแท่ง forward ครบ window หรือแก่เกิน hard_expire
        # (กันเคสค้างถาวรถ้า feed สั้นจริง — delisted / ข้อมูลขาด)
        hard_expire = max(min_age, _int_env("EVAL_HARD_EXPIRE_DAYS", lookahead * 3))
        ready = []
        for r in rows:
            if r.get("outcome") is not None or not r.get("atr") or not r.get("bar_date"):
                continue
            try:
                if (today - pd.Timestamp(r["bar_date"]).normalize()).days >= min_age:
                    ready.append(r)
            except Exception:  # noqa: BLE001
                continue
        ready = ready[:max_eval]
        if not ready:
            return 0
        by_sym = defaultdict(list)
        for r in ready:
            by_sym[(r["market"], r["symbol"])].append(r)
        evaluated = 0
        for (mk, sym), group in by_sym.items():
            df = fetch_history(mk, sym, crypto_exchange=cfg.crypto_exchange)
            if df is None or df.empty:
                continue
            idx = pd.DatetimeIndex([
                (pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo else pd.Timestamp(x)).normalize()
                for x in df.index
            ])
            highs = df["high"].astype(float).values
            lows = df["low"].astype(float).values
            for r in group:
                try:
                    bd = pd.Timestamp(r["bar_date"]).normalize()
                    pos = int(idx.searchsorted(bd))
                    if pos >= len(idx):
                        continue  # bar_date หลังแท่งสุดท้าย → ยังไม่มีแท่ง forward, ค้างไว้
                    # exact match: แท่งสัญญาณอยู่ที่ pos → forward เริ่ม pos+1
                    # ไม่ตรง (วันสัญญาณเป็นวันหยุด/feed revise ทิ้ง): idx[pos] = แท่งแรกหลัง bd = forward แท่งแรก
                    #   → เริ่มที่ pos (อย่าใช้ pos+1 จะข้ามแท่ง forward แรกไป = ทำผลเพี้ยน/win-rate ต่ำเกิน)
                    # pos==0 + ไม่ตรง = bd อยู่ก่อนทั้ง window → แท่งเข้าหายจริง ประเมินไม่ได้ ข้าม
                    if idx[pos] == bd:
                        start = pos + 1
                    elif pos == 0:
                        continue
                    else:
                        start = pos
                    fh = highs[start: start + lookahead]
                    fl = lows[start: start + lookahead]
                    if len(fh) == 0:
                        continue
                    entry = float(r["close"]); atr = float(r["atr"])
                    if atr <= 0:
                        continue
                    is_buy = r["direction"] == "buy"
                    tgt = entry + target_k * atr if is_buy else entry - target_k * atr
                    stp = entry - stop_k * atr if is_buy else entry + stop_k * atr
                    outcome, bars = "expired", len(fh)
                    for i in range(len(fh)):
                        hit_t = fh[i] >= tgt if is_buy else fl[i] <= tgt
                        hit_s = fl[i] <= stp if is_buy else fh[i] >= stp
                        if hit_t and hit_s:        # แตะทั้งเป้าและจุดตัดในแท่งเดียว → loss (อนุรักษ์)
                            outcome, bars = "loss", i + 1; break
                        if hit_t:
                            outcome, bars = "win", i + 1; break
                        if hit_s:
                            outcome, bars = "loss", i + 1; break
                    # ยังไม่ชน target/stop และเก็บแท่ง forward ไม่ครบ window → ปล่อยค้าง (outcome=None)
                    # ให้ประเมินใหม่รอบหน้าเมื่อมีแท่งครบ — กัน "expired" ด่วนช่วงวันหยุดเยอะ/แท่งล่าสุดยังไม่ออก
                    # (ทำ win-rate ต่ำกว่าจริง) · ยกเว้นแก่เกิน hard_expire = feed สั้นจริง → ยอมปิด expired
                    if outcome == "expired" and len(fh) < lookahead and (today - bd).days < hard_expire:
                        continue
                    # MFE/MAE วัดเฉพาะช่วง "ถือจริง" (ถึงแท่งที่ปิดผล) ไม่รวมแท่งหลังไม้ปิดไปแล้ว
                    # (expired → bars==len(fh) จึงเท่าเดิม)
                    fh_h, fl_h = fh[:bars], fl[:bars]
                    if is_buy:
                        mfe = (float(max(fh_h)) - entry) / atr; mae = (entry - float(min(fl_h))) / atr
                    else:
                        mfe = (entry - float(min(fl_h))) / atr; mae = (float(max(fh_h)) - entry) / atr
                    r["outcome"] = outcome
                    r["bars_to_outcome"] = bars
                    r["mfe_r"] = round(mfe, 2)
                    r["mae_r"] = round(mae, 2)
                    r["evaluated_at"] = _now_iso()
                    evaluated += 1
                except Exception as e:  # noqa: BLE001
                    log.debug("eval %s ข้าม: %s", r.get("id"), e)
        store.save_json(_LOG_FILE, rows)
        log.info("evaluate_outcomes: ประเมิน %d สัญญาณ (target +%.1fATR/stop -%.1fATR/%d แท่ง)",
                 evaluated, target_k, stop_k, lookahead)
        return evaluated
    except Exception as e:  # noqa: BLE001
        log.warning("evaluate_outcomes ล้มเหลว: %s", e)
        return 0


def calib_summary(cfg=None) -> str:
    """รายงาน win-rate เชิงประจักษ์จาก signal log (D2) — ป้อนคำสั่ง /calib (ไม่ใช้ cfg)"""
    try:
        from watchlist import store
    except Exception:  # noqa: BLE001
        return "อ่าน signal log ไม่ได้"
    rows = store.load_json(_LOG_FILE, [])
    if not isinstance(rows, list) or not rows:
        return "🔮 ยังไม่มี signal log — รอสแกนรอบถัดไปเริ่มบันทึก"
    done = [r for r in rows if r.get("outcome") in ("win", "loss")]
    pending = [r for r in rows if r.get("outcome") is None]
    expired = [r for r in rows if r.get("outcome") == "expired"]
    tgt = _float_env("EVAL_TARGET_ATR", 1.5); stp = _float_env("EVAL_STOP_ATR", 1.5)
    look = _int_env("EVAL_LOOKAHEAD_BARS", 10)
    if not done:
        return ("🔮 Calibration — ยังไม่มีผลพอประเมิน\n"
                f"บันทึกแล้ว {len(rows)} สัญญาณ · รอครบอายุ {len(pending)} · ไม่ถึงเป้า/ตัด {len(expired)}\n"
                "กลับมาดูใหม่หลังเก็บข้อมูล ~2 สัปดาห์")

    def wr(items) -> str:
        n = len(items)
        if not n:
            return "—"
        w = sum(1 for x in items if x.get("outcome") == "win")
        return f"{100.0 * w / n:.0f}% (n={n})"

    lines = [
        "🔮 Calibration — win-rate เชิงประจักษ์ (mechanical forward-test)",
        f"เกณฑ์: target +{tgt:g}×ATR ก่อน = ชนะ · stop -{stp:g}×ATR ก่อน = แพ้ · ใน {look} แท่ง",
        "",
        f"📊 รวมทั้งหมด: {wr(done)}",
        "",
        "ตามจำนวนดาว (ทดสอบว่าดาวเยอะ = แม่นจริงไหม):",
    ]
    for sc in range(5):
        b = [r for r in done if r.get("score") == sc]
        if b:
            lines.append(f"  {sc}⭐: {wr(b)}")
    hq = [r for r in done if r.get("high_quality")]
    nhq = [r for r in done if not r.get("high_quality")]
    lines += ["", f"HQ: {wr(hq)} · non-HQ: {wr(nhq)}",
              f"regime bull: {wr([r for r in done if r.get('regime_bull') is True])}"
              f" · bear: {wr([r for r in done if r.get('regime_bull') is False])}",
              f"buy: {wr([r for r in done if r.get('direction') == 'buy'])}"
              f" · sell: {wr([r for r in done if r.get('direction') == 'sell'])}",
              "", f"สถานะ: ประเมินแล้ว {len(done)} · รอ {len(pending)} · expired {len(expired)}",
              "⚠️ เป็น raw signal edge (ไม่หัก spread/commission) · ใช้เทียบเชิงสัมพัทธ์"]
    return "\n".join(lines)
