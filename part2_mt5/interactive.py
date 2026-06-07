"""
part2_mt5/interactive.py — โหมดโต้ตอบ: ส่งใบสั่ง + ปุ่มกดใน Telegram

ไหลงาน:
  สแกนโบรก → เสนอใบสั่ง "ทีละใบ" พร้อมปุ่ม [✅ เปิดออเดอร์] [❌ ไม่เปิด]
  · กดเปิด  → ยิงออเดอร์ MT5 (พร้อม SL/TP → วิ่งจน TP/SL เอง)
  · กดไม่เปิด หรือ เงียบเกิน 3 นาที → ลบใบสั่งทิ้ง
  · แล้วหาใบใหม่เรื่อย ๆ

ต้องใช้ "บอทตัวที่ 2" (TELEGRAM_BOT_TOKEN ใน config = บอทใหม่ ไม่ใช่ของ Part 1)
ยิงจริงเฉพาะ EXECUTE_ORDERS=true (ดีฟอลต์ false = โหมดทดสอบ ไม่ส่งออเดอร์จริง)
"""
from __future__ import annotations
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from _config import load
import mt5_client as m
import ticket as tk
import tg
import execute
import manage
import journal
import learn
import news_guard
from run import _watchlist

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("part2.interactive")

TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# ── ชื่อเทคนิคสำหรับแสดงใน Telegram ────────────────────────────────
_SRC_MAP = {
    "supertrend": "📈 SuperTrend",
    "halftrend":  "〰️ HalfTrend",
    "utbot":      "🤖 UT Bot",
    "hybrid":     "🔀 Hybrid-Pro",
    "scalp":      "⚡ EMA+Stoch",
    "fx_orb":     "🌅 FX ORB",
}
_TRADE_META = os.path.join(os.path.dirname(__file__), "part2_trade_meta.json")
_TRADE_META_MAX = 500   # เก็บแค่ N รายการล่าสุด (กันไฟล์ใหญ่เกิน)


def _save_trade_src(ticket: int, source: str) -> None:
    """บันทึก ticket → source เพื่อแสดงชื่อเทคนิคตอนปิดไม้"""
    try:
        data: dict = {}
        if os.path.exists(_TRADE_META):
            with open(_TRADE_META, "r", encoding="utf-8") as f:
                data = json.load(f)
        data[str(ticket)] = source
        # ตัดรายการเก่าออกถ้าเกิน limit
        if len(data) > _TRADE_META_MAX:
            keys = list(data.keys())
            data = {k: data[k] for k in keys[-_TRADE_META_MAX:]}
        with open(_TRADE_META, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:  # noqa: BLE001
        log.warning("save trade meta failed: %s", e)


def _load_trade_src() -> dict:
    """โหลด {ticket_str → source} — ใช้หาชื่อเทคนิคตอนรายงานปิดไม้"""
    try:
        if os.path.exists(_TRADE_META):
            with open(_TRADE_META, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # noqa: BLE001
        pass
    return {}


def _ensure_connected(cfg) -> bool:
    import MetaTrader5 as m5
    if m5.terminal_info() is not None:
        return True
    return m.connect(path=TERMINAL, login=int(cfg["MT5_LOGIN"]),
                     password=cfg["MT5_PASSWORD"], server=cfg["MT5_SERVER"])


def _scan_supertrend(cfg, broker: set) -> list:
    """#1 สแกน SuperTrend signal บน TF ที่กำหนด (default H1) — แทน CDC scan
    ตรวจ flip ล่าสุด (fresh_bars แท่ง) ไม่เอาสัญญาณค้างเก่า
    ATR-adaptive: SL ติดตาม SuperTrend line → ปรับตามความผันผวนจริง"""
    import scalp as _scalp
    import market_hours
    import pandas as pd
    tf = cfg.get("ST_TF", "H1")
    period = int(cfg.get("ST_PERIOD", "10"))
    mult = float(cfg.get("ST_MULT", "3.0"))
    fresh = int(cfg.get("ST_FRESH_BARS", "3"))
    rr = float(cfg.get("ST_RR", "2.0"))
    stale_min = 90 if tf in ("H1", "H4") else 45      # ยอมให้แท่งเก่าได้ตาม TF
    out = []
    for sym in _watchlist(cfg, broker):
        df = m.rates(sym, tf, 200)
        if df is None or len(df) < 100 or "time" not in df.columns:
            continue
        try:
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _scalp.supertrend_signal(df, period=period, mult=mult, fresh_bars=fresh)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "source": "supertrend",
                     "st_value": sig.get("st_value"), "rsi": None,
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"SuperTrend {tf} (×{mult})"}}, None))
    if out:
        log.info("supertrend(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_halftrend(cfg, broker: set) -> list:
    """#2 HalfTrend — smooth ATR trend, ลด whipsaw ดีกว่า SuperTrend ในตลาดผันผวน"""
    import scalp as _scalp
    import market_hours
    import pandas as pd
    tf = cfg.get("HT_TF", "H1")
    amp = int(cfg.get("HT_AMPLITUDE", "2"))
    dev = float(cfg.get("HT_CHANNEL_DEV", "2.0"))
    fresh = int(cfg.get("HT_FRESH_BARS", "3"))
    rr = float(cfg.get("HT_RR", "2.0"))
    stale_min = 90 if tf in ("H1", "H4") else 45
    out = []
    for sym in _watchlist(cfg, broker):
        df = m.rates(sym, tf, 200)
        if df is None or len(df) < 60 or "time" not in df.columns:
            continue
        try:
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _scalp.halftrend_signal(df, amplitude=amp, channel_dev=dev, fresh_bars=fresh)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "source": "halftrend",
                     "st_value": sig.get("ht_value"), "rsi": None,
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"HalfTrend {tf}"}}, None))
    if out:
        log.info("halftrend(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_utbot(cfg, broker: set) -> list:
    """#3 UT Bot Alerts — ATR trailing stop crossover, ตอบสนองไว เหมาะ M15/H1"""
    import scalp as _scalp
    import market_hours
    import pandas as pd
    tf = cfg.get("UTB_TF", "M15")
    kv = float(cfg.get("UTB_KEY_VALUE", "1.0"))
    ap = int(cfg.get("UTB_ATR_PERIOD", "10"))
    fresh = int(cfg.get("UTB_FRESH_BARS", "2"))
    rr = float(cfg.get("UTB_RR", "1.8"))
    stale_min = 45
    out = []
    for sym in _watchlist(cfg, broker):
        if market_hours.category(sym) == "fx":   # FX ขาดทุนใน backtest → เลี่ยง
            continue
        df = m.rates(sym, tf, 200)
        if df is None or len(df) < ap + 20 or "time" not in df.columns:
            continue
        try:
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _scalp.utbot_signal(df, key_value=kv, atr_period=ap, fresh_bars=fresh)
        if not sig.get("detected"):
            continue

        # ── ตรวจ H1 trend alignment ──────────────────────────────────────────
        # UT Bot ตอบสนองไว (M15) อาจสวนเทรนด์ใหญ่ (H1) → กรองด้วย SuperTrend H1
        # เหตุผล: ซื้อ M15 แต่ H1 ยังขาลง = เข้าสวนกระแส risky
        _st_tf_h1 = cfg.get("ST_TF", "H1")
        if tf.upper() != _st_tf_h1.upper():    # UT Bot TF ≠ H1 (เช่น M15)
            _df_h1 = m.rates(sym, _st_tf_h1, 200)
            if _df_h1 is not None and len(_df_h1) >= 100:
                _h1_st = _scalp.supertrend(
                    _df_h1,
                    period=int(cfg.get("ST_PERIOD", "10")),
                    mult=float(cfg.get("ST_MULT", "3.0")),
                )
                if _h1_st.get("direction") is not None:
                    _h1_dir = "buy" if int(_h1_st["direction"][-1]) == 1 else "sell"
                    if _h1_dir != sig["direction"]:
                        log.info("ข้าม %s — UT Bot(%s) %s ขัด H1 SuperTrend (%s)",
                                 sym, tf, sig["direction"], _h1_dir)
                        continue

        out.append(({"symbol": sym, "direction": sig["direction"], "source": "utbot",
                     "st_value": sig.get("ts_value"), "rsi": None,
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"UT Bot {tf} (kv={kv})"}}, None))
    if out:
        log.info("utbot(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_scalp(cfg, broker: set) -> list:
    """สแกน EMA+Stoch บน M15 (เฉพาะของผันผวน เลี่ยง FX ตามผล backtest) → [(bias, None)]
    เติมจังหวะ 'ตามเทรนด์' ระหว่างรอสัญญาณหลัก — ยังต้องผ่านเกราะ build_ticket ทุกด่าน
    bias.scalp = {sl, rr, tag} → build_ticket ใช้ SL/TP ของกลยุทธ์เอง (ปิดไว ไม่โดนกฎ +2%)"""
    import scalp as _scalp
    import market_hours
    import pandas as pd
    rr = float(cfg.get("SCALP_RR", "1.8"))
    wk = (datetime.now(timezone.utc).weekday() >= 5 and
          cfg.get("SCALP_WEEKEND_LOOSEN", "true").lower() in ("1", "true", "yes", "on"))
    os_lvl, ob_lvl = (30.0, 70.0) if wk else (20.0, 80.0)   # เสาร์-อาทิตย์: ผ่อน Stoch ให้ไวขึ้น
    out = []
    for sym in _watchlist(cfg, broker):
        if market_hours.category(sym) == "fx":          # FX ขาดทุนใน backtest → เลี่ยง
            continue
        df = m.rates(sym, "M15", 260)
        if df is None or len(df) < 210 or "time" not in df.columns:
            continue
        try:                                             # ไม่มีแท่ง M15 ใหม่ = ตลาดปิด → ข้าม
            # แปลง last_t ให้เป็น aware datetime (UTC) เพื่อเทียบกับ now(timezone.utc) ได้ถูกต้อง
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > 45:
                continue
        except Exception:  # noqa: BLE001
            pass
        sl_mult = float(cfg.get("SCALP_SL_ATR_MULT", "0.6"))
        sig = _scalp.ema_ribbon_stoch(df, oversold=os_lvl, overbought=ob_lvl, sl_atr_mult=sl_mult)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "zone": None, "rsi": None,
                     "source": "scalp",
                     "scalp": {"sl": sig["sl"], "rr": rr, "tag": "EMA+Stoch M15"}}, None))
    if out:
        log.info("scalp(EMA+Stoch M15): เจอ %d สัญญาณ", len(out))
    return out


def _scan_fx_orb(cfg, broker: set) -> list:
    """สแกน Asian-London ORB เฉพาะคู่เงิน (รันเฉพาะหน้าต่าง London 07-11 UTC) → [(bias, None)]
    TP/SL สัมบูรณ์ของกลยุทธ์ (TP=ความกว้างกรอบ · SL=กึ่งกลาง) ส่งผ่าน bias.scalp"""
    if not (7 <= datetime.now(timezone.utc).hour < 11):   # นอกหน้าต่าง London → ไม่ต้องสแกน
        return []
    import scalp as _scalp
    import market_hours
    out = []
    for sym in _watchlist(cfg, broker):
        if market_hours.category(sym) != "fx":
            continue
        df = m.rates(sym, "M15", 60)
        if df is None or len(df) < 30 or "time" not in df.columns:
            continue
        sig = _scalp.asian_london_orb(df)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "zone": None, "rsi": None,
                     "source": "fx_orb",
                     "scalp": {"sl": sig["sl"], "tp": sig["tp"], "tag": "FX ORB (London)"}}, None))
    if out:
        log.info("FX ORB (London): เจอ %d สัญญาณ", len(out))
    return out


def _scan_hybrid(cfg, broker: set) -> list:
    """สแกน Hybrid-Pro (H1 เทรนด์ + M15 ย่อ EMA20 + RSI 40-60 + แท่งกลับตัว) เลี่ยง FX → [(bias,None)]
    spread guard กัน SOL/XRP (spread สูง) ให้เองในขั้น build_ticket"""
    import scalp as _scalp
    import market_hours
    import pandas as pd
    rr = float(cfg.get("HYBRID_RR", "2.5"))
    wk = (datetime.now(timezone.utc).weekday() >= 5 and
          cfg.get("SCALP_WEEKEND_LOOSEN", "true").lower() in ("1", "true", "yes", "on"))
    rlo, rhi = (35.0, 65.0) if wk else (40.0, 60.0)        # เสาร์-อาทิตย์: ผ่อน RSI band ให้ไวขึ้น
    out = []
    for sym in _watchlist(cfg, broker):
        if market_hours.category(sym) == "fx":
            continue
        df = m.rates(sym, "M15", 900)                    # ต้องมากพอ resample H1 EMA200
        if df is None or len(df) < 850 or "time" not in df.columns:
            continue
        try:
            # แปลง last_t ให้เป็น aware datetime (UTC) เพื่อเทียบกับ now(timezone.utc) ได้ถูกต้อง
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > 45:
                continue
        except Exception:  # noqa: BLE001
            pass
        sl_mult = float(cfg.get("HYBRID_SL_ATR_MULT", "0.5"))
        sig = _scalp.hybrid_pro(df, rr=rr, rsi_lo=rlo, rsi_hi=rhi, sl_atr_mult=sl_mult)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "zone": None, "rsi": None,
                     "source": "hybrid",
                     "scalp": {"sl": sig["sl"], "tp": sig["tp"], "tag": "Hybrid-Pro"}}, None))
    if out:
        log.info("Hybrid-Pro: เจอ %d สัญญาณ", len(out))
    return out


def _check_metal_unlock(cfg, token, chat, bal: float, state: dict) -> None:
    """แจ้งเตือน + ปลดล็อกโลหะอัตโนมัติเมื่อพอร์ตถึงเกณฑ์ (เด้งเตือนครั้งเดียว/รอบขึ้น)"""
    if not bal or bal <= 0:
        return
    for name, key, cfg_key, emoji in (
            ("ทอง (XAUUSD)", "gold_unlocked", "GOLD_MIN_BALANCE", "🥇"),
            ("เงิน (XAGUSD)", "silver_unlocked", "SILVER_MIN_BALANCE", "🥈")):
        thr = float(cfg.get(cfg_key, "0") or "0")
        if thr <= 0:
            continue
        if bal >= thr and not state.get(key):
            state[key] = True
            _save_state(state)
            tg.send_text(token, chat,
                         f"🔓 {emoji} ปลดล็อก {name}! พอร์ตถึง ${bal:,.0f} (เกณฑ์ ${thr:,.0f}) "
                         f"— บอทเริ่มพิจารณาเทรด{name.split()[0]}ได้แล้ว")
            log.info("ปลดล็อก %s ที่พอร์ต $%.0f", name, bal)
        elif bal < thr * 0.95 and state.get(key):     # ตกต่ำกว่าเกณฑ์พอควร → re-arm เงียบ ๆ
            state[key] = False
            _save_state(state)


def _summary(t: dict) -> str:
    dir_th = "🟢 Buy" if t["direction"] == "buy" else "🔴 Sell"
    return f"📋 {t['exsym']} {dir_th}"


def _strat_tags(t: dict) -> str:
    """ป้ายกลยุทธ์/ขนาดไม้ ที่ทำให้เข้าออเดอร์ (โชว์ในสรุปสแกน)"""
    tags = []
    if t.get("scalp"):
        tags.append(t["scalp"])
    if t.get("reduced"):
        tags.append("ไม้เล็ก")
    for k, lbl in (("three_bar", "3BP"), ("brt", "BRT"), ("ibb", "IBB"), ("tlp", "2Leg")):
        if (t.get(k) or {}).get("detected"):
            tags.append(lbl)
    return " · ".join(tags)


def _scan_summary(results: list) -> str:
    """สรุปผลสแกน 1 รอบ → ข้อความเดียว (ตัวที่เปิด + ตัวที่ข้าม พร้อมเหตุผล)"""
    n_open = sum(1 for r in results if r[2] == "open")
    lines = [f"🔍 สแกน: เปิด {n_open} · พิจารณา {len(results)} ตัว"]
    for sym, dr, status, detail in results:
        side = "🟢buy" if dr == "buy" else "🔴sell"
        if status == "open":
            lines.append(f"✅ เปิด {sym} {side}" + (f" — {detail}" if detail else ""))
        else:
            lines.append(f"⛔ ข้าม {sym} {side} — {detail}")
    return "\n".join(lines)


def _do_open(cfg, token, chat, p, execute_on: bool) -> None:
    t = p["ticket"]
    sz = t.get("sizing")
    if not execute_on:
        tg.edit_text(token, chat, p["msg_id"],
                     _summary(t) + "\n\n✅ (โหมดทดสอบ) ยืนยันเปิดแล้ว — ตั้ง EXECUTE_ORDERS=true เพื่อยิงจริง")
        return
    if not sz or sz["lots"] <= 0:
        tg.edit_text(token, chat, p["msg_id"], _summary(t) + "\n\n❌ lot=0 (ยอดเงินไม่พอ)")
        return
    res = execute.place_order(t["exsym"], t["direction"], sz["lots"], t["sl"], t["tp"])
    if res.get("ok"):
        learn.record_entry(res.get("ticket"), t)   # เก็บฟีเจอร์ไว้เรียนรู้ภายหลัง
        _save_trade_src(res["ticket"], (t.get("bias") or {}).get("source", ""))  # บันทึก ticket→technique
        tg.edit_text(token, chat, p["msg_id"], _summary(t) +
                     f"\n\n✅ เปิดออเดอร์แล้ว! #{res.get('ticket')} @ {res.get('price')}"
                     f"\nLot {sz['lots']} · SL/TP ตั้งให้แล้ว — MT5 จะปิดเองที่ TP/SL")
        log.info("เปิดออเดอร์ %s %s lot %s → ticket %s", t["exsym"], t["direction"], sz["lots"], res.get("ticket"))
    else:
        tg.edit_text(token, chat, p["msg_id"], _summary(t) + f"\n\n❌ เปิดไม่สำเร็จ: {res.get('comment')}")
        log.warning("เปิดออเดอร์ %s ล้มเหลว: %s", t["exsym"], res.get("comment"))


def _stats_text() -> str:
    s = journal.compute_stats()
    if not s:
        return "📒 Part 2 ยังไม่มีไม้ที่ปิด — สถิติจะขึ้นหลังมีไม้ปิดไม้แรก"
    lines = ["📊 สถิติ Part 2 (ไม้ที่ปิดแล้ว)",
             f"• จำนวน: {s['trades']} ไม้",
             f"• Win rate: {s['win_rate']}%",
             f"• กำไรรวม: ${s['total']}",
             f"• เฉลี่ย/ไม้: ${s['avg']}"]
    if s["pf"] is not None:
        lines.append(f"• Profit factor: {s['pf']} {'✅ มีเอจ' if s['pf'] > 1 else '⚠️ ติดลบ'}")
    lines.append(f"• ดีสุด/แย่สุด: ${s['best']} / ${s['worst']}")
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "🤖 คำสั่ง Part 2 (MT5 Auto-Trading)\n\n"
        "/status — สถานะสด: โหมด · พอร์ต · P/L วันนี้ · ไม้ที่เปิด\n"
        "/stats — สถิติผลเทรดสะสม (win rate · profit factor)\n"
        "/insights — 🧠 บทเรียน: เทคนิคไหนได้เงินจริง (บอทเรียนรู้จากผลจริง)\n"
        "/pause — ⏸️ หยุดเปิดไม้ใหม่ชั่วคราว (ไม้เก่ายังจัดการต่อ)\n"
        "/resume — ▶️ กลับมาเปิดไม้อัตโนมัติ\n"
        "/closeall — 🧹 ปิดไม้ Part 2 ทั้งหมดทันที (ฉุกเฉิน)\n"
        "/update — ⬇️ ดึงโค้ดใหม่จาก GitHub แล้ว restart (ใช้ทุกครั้งที่อัปเดต)\n"
        "/stop — 🛑 หยุดบอท (ไม้ที่เปิดอยู่ยังคงเปิดใน MT5)\n"
        "/restart — 🔄 Restart บอท (ไม่ดึงโค้ดใหม่)\n"
        "/help — รายการคำสั่งนี้\n\n"
        "🔁 โหมด Auto: บอทสแกน → ตัดสินใจ → ยิงออเดอร์เอง → รายงานที่นี่\n"
        "   เปิดไม้ใหม่เมื่อผ่านด่าน: SuperTrend/HalfTrend/UT Bot + แท่งเทียน/วอลุ่ม + Gemini + เกราะความเสี่ยง\n\n"
        "ℹ️ จัดการเอง: TP +2% ของราคา · +1R เลื่อน SL เท่าทุน · เลี่ยงข่าวแรง\n"
        "🛡️ เกราะ: เบรกขาดทุนวัน + ขาดทุนสะสม (drawdown) · จำกัดไม้ทิศเดียว · สรุปประจำวัน"
    )


def _status_text(auto_on: bool, execute_on: bool) -> str:
    import MetaTrader5 as m5
    acc = m.account() or {}
    poss = [p for p in (m5.positions_get() or []) if p.magic == 260605]
    if auto_on:
        mode = "🔁 Auto (ยิงจริง)" if execute_on else "🔁 Auto (ทดสอบ ไม่ยิงจริง)"
    else:
        mode = "✋ Manual (กดปุ่มเอง)"
    if _is_paused():
        mode += " · ⏸️ หยุดเปิดไม้ใหม่"
    lines = ["🤖 Part 2 — ทำงานอยู่ ✅",
             f"โหมด: {mode}",
             f"💼 พอร์ต: ${acc.get('balance', 0):,.2f} {acc.get('currency', '')} "
             f"(equity ${acc.get('equity', 0):,.2f})",
             f"📈 P/L วันนี้: ${journal.today_pnl():+.2f}",
             f"📂 ไม้ที่เปิด: {len(poss)}"]
    ov = learn.overview()
    lines.append(f"🧠 เรียนรู้: ปิดแล้ว {ov['n']} ไม้" + (f" · ชนะ {ov['win_rate']:.0f}%" if ov['n'] else ""))
    for p in poss:
        side = "Buy" if p.type == 0 else "Sell"
        lines.append(f"   • {p.symbol} {side} {p.volume} · ${p.profit:+.2f}")
    return "\n".join(lines)


_PAUSE = __import__("os").path.join(__import__("os").path.dirname(__file__), "part2_paused.flag")
_SHOULD_RUN = __import__("os").path.join(__import__("os").path.dirname(__file__), "part2_should_run.flag")


def _is_paused() -> bool:
    import os
    return os.path.exists(_PAUSE)


def _set_pause(on: bool) -> None:
    """หยุด/เริ่มเปิดไม้ใหม่ — เก็บเป็นไฟล์ flag เพื่อให้สถานะอยู่รอดแม้บอทรีสตาร์ท"""
    import os
    if on:
        open(_PAUSE, "w").close()
    else:
        try:
            os.remove(_PAUSE)
        except OSError:
            pass


def _count_open() -> int:
    import MetaTrader5 as m5
    return len([p for p in (m5.positions_get() or []) if p.magic == 260605])


def _open_symbols() -> set:
    """ชื่อ symbol ที่ Part 2 มีไม้เปิดอยู่ (กันเปิดซ้ำตัวเดิม)"""
    import MetaTrader5 as m5
    return {p.symbol for p in (m5.positions_get() or []) if p.magic == 260605}


def _open_report(t: dict, res: "dict | None") -> str:
    """ข้อความรายงานตอนบอทเปิดไม้อัตโนมัติ (res=None = โหมดทดสอบ)"""
    sz = t.get("sizing") or {}
    dir_th = "🟢 Buy" if t["direction"] == "buy" else "🔴 Sell"

    def fx(x):
        return f"{x:,.5f}".rstrip("0").rstrip(".") if x is not None else "—"

    head = "🤖 เปิดออเดอร์อัตโนมัติ" if res else "🧪 (ทดสอบ) บอทจะเปิด"
    lines = [f"{head} — {t['exsym']} {dir_th}"]
    # ชื่อเทคนิคที่ใช้เปิดไม้ (tag มี TF ครบ เช่น "SuperTrend H1 (×3.0)")
    _b0 = t.get("bias") or {}
    _tag = (_b0.get("scalp") or {}).get("tag") or _SRC_MAP.get(_b0.get("source", ""), "")
    if _tag:
        lines.append(f"⚙️ {_tag}")
    if res and res.get("ticket"):
        lines.append(f"#{res['ticket']} @ {fx(res.get('price'))}")
    lines.append(f"🎯 Entry {fx(t['spot'])} · 🛡️ SL {fx(t['sl'])} · 🎯 TP {fx(t['tp'])}"
                 + (f" · R:R {t['rr']:.2f}" if t.get("rr") else ""))
    if t.get("three_bar", {}).get("detected"):
        lines.append("🔥 จังหวะ 3-Bar Play (โมเมนตัมต่อเทรนด์ · SL ใต้แท่งพัก)")
    if t.get("brt", {}).get("detected"):
        lines.append("🔁 Breakout & Retest (เบรกแนว→ย่อ retest→กลับตัว)")
    if t.get("ibb", {}).get("detected"):
        lines.append("📦 Inside Bar Breakout (สะสมแล้วเบรก)")
    if t.get("tlp", {}).get("detected"):
        lines.append("📐 2-Legged Pullback (ย่อ 2 จังหวะตามเทรนด์)")
    _b = t.get("bias") or {}
    if _b.get("atr_pct"):
        rm = f" · ขยับ {_b['recent_move_pct']}%/12ชม" if _b.get("recent_move_pct") is not None else ""
        lines.append(f"⚡ คึกคัก: ATR {_b['atr_pct']}%/วัน{rm} · วอลุ่ม {_b.get('vol_ratio', '?')}×")
    _sr = t.get("sr") or {}
    if t["direction"] == "buy" and _sr.get("near_resistance"):
        lines.append("⚠️ ใกล้แนวต้าน — ระวัง")
    elif t["direction"] == "sell" and _sr.get("near_support"):
        lines.append("⚠️ ใกล้แนวรับ — ระวัง")
    if sz:
        lines.append(f"📦 Lot {sz.get('lots')} · เสี่ยงจริง ${sz.get('risk_money')}"
                     + (f" ({sz.get('actual_pct')}% ของพอร์ต)" if sz.get("actual_pct") is not None else ""))
    if t.get("reduced"):
        lines.append("⚠️ ไม้เล็ก (AI ขอระวัง จึงลดขนาด)")
    v = t.get("verdict") or {}
    if v.get("reason"):
        lines.append("🤖 " + v["reason"])
    return "\n".join(lines)


def _auto_open(cfg, token, chat, t, execute_on: bool) -> bool:
    """ยิงออเดอร์อัตโนมัติ + รายงาน Telegram — คืน True ถ้าเปิด/รายงานสำเร็จ"""
    sz = t.get("sizing")
    if not sz or sz.get("lots", 0) <= 0:
        return False
    if not execute_on:                       # โหมดทดสอบ: รายงานว่า "จะเปิด" แต่ไม่ยิงจริง
        tg.send_text(token, chat, _open_report(t, None))
        return True
    res = execute.place_order(t["exsym"], t["direction"], sz["lots"], t["sl"], t["tp"])
    if res.get("ok"):
        learn.record_entry(res.get("ticket"), t)   # เก็บฟีเจอร์ไว้เรียนรู้ภายหลัง
        _save_trade_src(res["ticket"], (t.get("bias") or {}).get("source", ""))  # บันทึก ticket→technique
        tg.send_text(token, chat, _open_report(t, res))
        log.info("AUTO เปิด %s %s lot %s → #%s", t["exsym"], t["direction"], sz["lots"], res.get("ticket"))
        return True
    tg.send_text(token, chat, f"⚠️ เปิดอัตโนมัติไม่สำเร็จ {t['exsym']}: {res.get('comment')}")
    log.warning("AUTO เปิด %s ล้มเหลว: %s", t["exsym"], res.get("comment"))
    return False


def _close_all(token, chat) -> None:
    """ปิดไม้ Part 2 ทั้งหมดทันที (คำสั่งฉุกเฉิน /closeall)"""
    import MetaTrader5 as m5
    poss = [p for p in (m5.positions_get() or []) if p.magic == 260605]
    if not poss:
        tg.send_text(token, chat, "ℹ️ ไม่มีไม้ Part 2 เปิดอยู่")
        return
    ok = 0
    tot = 0.0
    for p in poss:
        tot += p.profit
        if execute.close_position(p).get("ok"):
            ok += 1
    tg.send_text(token, chat, f"🧹 ปิดทั้งหมด {ok}/{len(poss)} ไม้ · P/L รวม ${tot:+.2f}")
    log.info("closeall: ปิด %d/%d ไม้", ok, len(poss))


_STATE_FILE = __import__("os").path.join(__import__("os").path.dirname(__file__), "part2_state.json")


def _load_state() -> dict:
    import os
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r") as f:
                return json.load(f) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_state(d: dict) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(d, f)
    except Exception:  # noqa: BLE001
        pass


def _count_direction(direction: str) -> int:
    """จำนวนไม้ Part 2 ที่เปิดอยู่ในทิศเดียวกัน (กันเปิดทิศเดียวเยอะเกิน)"""
    import MetaTrader5 as m5
    want = 0 if direction == "buy" else 1
    return len([p for p in (m5.positions_get() or []) if p.magic == 260605 and p.type == want])


def _count_group(group: str) -> int:
    """จำนวนไม้ในกลุ่มสินทรัพย์เดียวกัน (กระจายความเสี่ยง — ไม่กระจุกกลุ่มเดียว)"""
    import MetaTrader5 as m5
    import market_hours
    return len([p for p in (m5.positions_get() or []) if p.magic == 260605
                and market_hours.correlation_group(p.symbol) == group])


def _daily_digest_text() -> str:
    """สรุปประจำวัน — ส่ง Telegram ครั้งเดียว/วัน"""
    import MetaTrader5 as m5
    acc = m.account() or {}
    poss = [p for p in (m5.positions_get() or []) if p.magic == 260605]
    lines = [f"📊 สรุปประจำวัน Part 2 · {datetime.now().strftime('%d/%m')}",
             f"💼 พอร์ต ${acc.get('balance', 0):,.2f} (equity ${acc.get('equity', 0):,.2f})",
             f"📈 P/L วันนี้: ${journal.today_pnl():+.2f}",
             f"📂 ไม้เปิดค้าง: {len(poss)}"]
    for p in poss:
        side = "Buy" if p.type == 0 else "Sell"
        lines.append(f"   • {p.symbol} {side} {p.volume} · ${p.profit:+.2f}")
    s = journal.compute_stats()
    if s:
        pf = f" · PF {s['pf']}" if s.get("pf") is not None else ""
        lines.append(f"🎯 สะสม: {s['trades']} ไม้ · ชนะ {s['win_rate']}%{pf} · รวม ${s['total']:+.2f}")
    return "\n".join(lines)


_LOCK = __import__("os").path.join(__import__("os").path.dirname(__file__), "part2.lock")


_lock_fh = None


def _acquire_lock() -> bool:
    """กันรันซ้ำด้วย file lock — OS ปลดล็อกให้เองเมื่อ process ตาย (crash/ไฟดับ/ถูก kill)
    ไม่มีปัญหา 'lock ค้าง PID เก่า' อีก"""
    global _lock_fh
    try:
        import msvcrt
        _lock_fh = open(_LOCK, "w")
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False                        # อีก instance ถือ lock อยู่
    except Exception:  # noqa: BLE001
        return True                         # lock ใช้ไม่ได้ → ปล่อยรัน (ดีกว่าบล็อก)


def main():
    cfg = load()
    if not _acquire_lock():
        log.warning("Part 2 มี instance รันอยู่แล้ว — ออก (กันรันซ้ำ)")
        sys.exit(2)                         # exit code 2 = รันซ้ำ → bat จะไม่ relaunch
    token = cfg.get("TELEGRAM_BOT_TOKEN", "")
    chat = cfg.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.error("ไม่มี TELEGRAM_BOT_TOKEN/CHAT_ID (ต้องเป็นบอทตัวที่ 2)")
        return
    ttl = int(cfg.get("TICKET_TTL_SEC", "180"))
    scan_gap = int(float(cfg.get("SCAN_INTERVAL_MIN", "5")) * 60)
    cooldown = int(cfg.get("SYMBOL_COOLDOWN_SEC", "3600"))
    execute_on = cfg.get("EXECUTE_ORDERS", "false").lower() in ("1", "true", "yes", "on")
    auto_on = cfg.get("AUTO_TRADE", "false").lower() in ("1", "true", "yes", "on")
    use_supertrend = cfg.get("USE_SUPERTREND", "true").lower() in ("1", "true", "yes", "on")   # SuperTrend H1
    use_halftrend  = cfg.get("USE_HALFTREND",  "true").lower() in ("1", "true", "yes", "on")   # HalfTrend H1
    use_utbot      = cfg.get("USE_UTBOT",      "true").lower() in ("1", "true", "yes", "on")   # UT Bot M15
    use_ema_stoch  = cfg.get("USE_EMA_STOCH", "false").lower() in ("1", "true", "yes", "on")   # scalp EMA+Stoch M15
    use_fx_orb = cfg.get("USE_FX_ORB", "false").lower() in ("1", "true", "yes", "on")        # Asian-London ORB เฉพาะ FX
    use_hybrid = cfg.get("USE_HYBRID_PRO", "false").lower() in ("1", "true", "yes", "on")    # Hybrid-Pro (H1 trend + M15 pullback)
    max_pos = int(cfg.get("MAX_OPEN_POSITIONS", "5"))
    notify_scan = cfg.get("NOTIFY_SCAN", "true").lower() in ("1", "true", "yes", "on")
    max_dd = float(cfg.get("MAX_DRAWDOWN_PCT", "0") or "0")        # เบรกขาดทุนสะสม (0=ปิด)
    max_per_dir = int(cfg.get("MAX_PER_DIRECTION", "0") or "0")    # จำกัดไม้ทิศเดียวกัน (0=ไม่จำกัด)
    max_per_grp = int(cfg.get("MAX_PER_GROUP", "1") or "0")        # ไม้/กลุ่ม ดีฟอลต์ (จ-ศ · 0=ไม่จำกัด)
    max_per_grp_us = int(cfg.get("MAX_PER_GROUP_US", "2") or "0")  # กลุ่ม "หุ้น/ดัชนี US" เปิดได้กี่ไม้ (US มีหลายตัว)
    digest_hour = int(cfg.get("DIGEST_HOUR", "8"))                # ชั่วโมงส่งสรุปประจำวัน
    _state = _load_state()
    peak_eq = float(_state.get("peak_eq", 0) or 0)
    accept = {x.strip() for x in cfg.get("GEMINI_ACCEPT", "enter,small").split(",") if x.strip()}
    finnhub = cfg.get("FINNHUB_API_KEY", "")
    blackout_min = int(cfg.get("BLACKOUT_MIN", "30"))
    max_daily_loss = float(cfg.get("MAX_DAILY_LOSS_PCT", "4.0"))

    if not _ensure_connected(cfg):
        log.error("ต่อ MT5 ไม่ได้"); return
    tg.set_commands(token)       # ลงเมนูคำสั่งใน Telegram
    journal.record_closed()      # seed เงียบ ๆ ตอนเริ่ม (กันรายงานไม้เก่าย้อนหลังตอนบูต)
    mode_txt = (("🔁 Auto ยิงจริง" if execute_on else "🔁 Auto ทดสอบ") if auto_on else "✋ Manual กดปุ่ม")
    log.info("เริ่ม Part 2 · โหมด=%s · TTL=%ds · maxpos=%d", mode_txt, ttl, max_pos)
    tg.send_text(token, chat, f"🤖 Part 2 เริ่มทำงาน · โหมด {mode_txt}\n"
                              "พิมพ์ /help ดูคำสั่ง · /pause หยุดชั่วคราว · /closeall ปิดไม้ทั้งหมด")

    # ── Drain pending Telegram messages ─────────────────────────────────────
    # ทุกครั้งที่บอทเริ่มใหม่ offset=0 → Telegram ส่ง command เก่า (/stop /restart) กลับมา
    # แก้: อ่านทิ้งทั้งหมดก่อน แล้วตั้ง offset ไปที่ message ล่าสุด
    # ผล: บอทเริ่มรับ command "ใหม่เท่านั้น" หลังจากบูตแล้ว
    offset = 0
    _old = tg.get_updates(token, 0)
    if _old:
        offset = _old[-1]["update_id"] + 1
        tg.ack_updates(token, offset)
        log.info("drained %d pending Telegram updates (offset→%d)", len(_old), offset)

    queue: list = []
    pending = None
    last_scan = 0.0
    last_journal = 0.0
    counter = 0
    recent: dict = {}
    daily_halt = False
    disconnected = False
    prev_open = 0          # นับไม้เปิด รอบก่อน — ถ้าลดลง = มีไม้ปิด → สแกนหาตัวใหม่ทันที
    last_scan_key = None   # กันส่งสรุปสแกนซ้ำ (ถ้าผลเหมือนเดิม)

    while True:
        try:
            if not os.path.exists(_SHOULD_RUN):       # กดปิดบอท (ลบ flag) → ปิดอย่างปลอดภัย
                log.info("🛑 หยุดบอท (ไม่มี flag part2_should_run — กดปิดจาก Stop)")
                break
            now = time.time()
            # 1) รับ updates: ปุ่มกด (callback) + คำสั่งข้อความ (/status /stats)
            for upd in tg.get_updates(token, offset):
                offset = upd["update_id"] + 1
                cb = upd.get("callback_query")
                msg = upd.get("message")
                if cb:
                    tg.answer_callback(token, cb["id"])
                    action, _, tid = cb.get("data", "").partition(":")
                    if not pending or pending["tid"] != tid:
                        try:
                            tg.delete_msg(token, chat, cb["message"]["message_id"])
                        except Exception:  # noqa: BLE001
                            pass
                        continue
                    if action == "open":
                        _do_open(cfg, token, chat, pending, execute_on)
                    else:
                        tg.delete_msg(token, chat, pending["msg_id"])
                    pending = None
                elif msg:
                    cmd = (msg.get("text") or "").strip().lower().lstrip("/").split("@")[0]
                    if cmd in ("help", "start"):
                        tg.send_text(token, chat, _help_text())
                    elif cmd == "status" and _ensure_connected(cfg):
                        tg.send_text(token, chat, _status_text(auto_on, execute_on))
                    elif cmd == "stats":
                        tg.send_text(token, chat, _stats_text())
                    elif cmd == "insights":
                        try:
                            learn.attach_outcomes()
                        except Exception:  # noqa: BLE001
                            pass
                        tg.send_text(token, chat, learn.edge_report())
                    elif cmd == "pause":
                        _set_pause(True)
                        tg.send_text(token, chat, "⏸️ หยุดเปิดไม้ใหม่แล้ว — ไม้ที่เปิดอยู่ยังจัดการต่อ (พิมพ์ /resume เพื่อเริ่มใหม่)")
                    elif cmd == "resume":
                        _set_pause(False)
                        tg.send_text(token, chat, "▶️ กลับมาเปิดไม้อัตโนมัติแล้ว")
                    elif cmd == "closeall" and _ensure_connected(cfg):
                        _close_all(token, chat)
                    elif cmd == "update":
                        # git pull จากภายในบอท → restart → โค้ดใหม่โหลดทันที ไม่ต้อง SSH
                        import subprocess
                        tg.send_text(token, chat, "⬇️ กำลัง git pull จาก GitHub ...")
                        try:
                            _repo = os.path.dirname(os.path.abspath(__file__))
                            _res = subprocess.run(
                                ["git", "-C", _repo, "pull", "--ff-only"],
                                capture_output=True, text=True, timeout=30,
                            )
                            _out = (_res.stdout or "").strip() or "(ไม่มี output)"
                            if _res.returncode == 0:
                                tg.send_text(token, chat,
                                             f"✅ git pull สำเร็จ\n{_out}\n\n🔄 กำลัง restart...")
                            else:
                                _err = (_res.stderr or "").strip()
                                tg.send_text(token, chat,
                                             f"⚠️ git pull มีปัญหา:\n{_err}\n\n🔄 Restart ด้วยโค้ดเดิม")
                        except Exception as _e:
                            tg.send_text(token, chat,
                                         f"❌ git pull ล้มเหลว: {_e}\n🔄 Restart ด้วยโค้ดเดิม")
                        tg.ack_updates(token, offset)  # ยืนยัน update กับ Telegram ก่อน exit
                        time.sleep(2)
                        sys.exit(0)   # wrapper (start_loop.bat / start_bot.sh) restart อัตโนมัติ
                    elif cmd == "stop":
                        # ลบ flag → start_loop.bat ตรวจเจอ "ไม่มี flag" → goto end → ไม่ restart
                        tg.send_text(token, chat,
                                     "🛑 หยุดบอทแล้ว\n"
                                     "ไม้ที่เปิดอยู่ยังคงเปิดต่อใน MT5 ✅\n"
                                     "Restart: รัน start_loop.bat หรือพิมพ์ /restart ใน Telegram")
                        if os.path.exists(_SHOULD_RUN):
                            os.remove(_SHOULD_RUN)
                        tg.ack_updates(token, offset)  # ยืนยัน update กับ Telegram ก่อน exit
                        time.sleep(2)       # รอให้ message ส่งก่อน exit
                        sys.exit(0)
                    elif cmd == "restart":
                        # ไม่ลบ flag → start_loop.bat ตรวจเจอ flag ยังอยู่ → restart อัตโนมัติใน ~15 วิ
                        tg.send_text(token, chat,
                                     "🔄 กำลัง restart บอท...\n"
                                     "รอ ~15 วิ บอทจะกลับมาและแจ้ง Telegram ✅")
                        tg.ack_updates(token, offset)  # ยืนยัน update กับ Telegram ก่อน exit
                        time.sleep(2)       # รอให้ message ส่งก่อน exit
                        sys.exit(0)

            # 2) หมดเวลา → ลบใบ
            if pending and now - pending["sent_at"] > ttl:
                tg.delete_msg(token, chat, pending["msg_id"])
                log.info("ใบสั่ง %s หมดเวลา — ลบ", pending["ticket"]["exsym"])
                pending = None

            # ต่อ MT5 + แจ้งเตือนถ้าหลุด
            if not _ensure_connected(cfg):
                if not disconnected:
                    tg.send_text(token, chat, "⚠️ Part 2: ต่อ MT5 ไม่ได้ — หยุดชั่วคราว จะลองใหม่")
                    disconnected = True
                time.sleep(5); continue
            if disconnected:
                tg.send_text(token, chat, "✅ Part 2: เชื่อม MT5 กลับมาแล้ว")
                disconnected = False

            acc_now = m.account() or {}
            bal = acc_now.get("balance", 0)
            eq = acc_now.get("equity", bal) or bal
            _check_metal_unlock(cfg, token, chat, bal, _state)   # ปลดล็อกทอง/เงินเมื่อพอร์ตถึงเกณฑ์

            # 2.4) เบรกขาดทุนสะสม (drawdown จากจุดสูงสุด) → หยุดหมด + ปิดไม้ + แจ้ง
            if max_dd > 0 and eq > 0:
                if eq > peak_eq:
                    peak_eq = eq
                    _state["peak_eq"] = peak_eq; _save_state(_state)
                elif peak_eq > 0 and eq <= peak_eq * (1 - max_dd / 100) and not _is_paused():
                    dd = (peak_eq - eq) / peak_eq * 100
                    tg.send_text(token, chat, f"🛑 ขาดทุนสะสมถึง {dd:.1f}% (เพดาน {max_dd:.0f}%) — "
                                              "หยุดทั้งหมด + ปิดไม้ · พิมพ์ /resume เพื่อเริ่มใหม่")
                    if execute_on:
                        _close_all(token, chat)
                    _set_pause(True)
                    peak_eq = eq                    # reset baseline กัน resume แล้วเด้งซ้ำ
                    _state["peak_eq"] = peak_eq; _save_state(_state)

            # 2.5) จัดการ position ที่เปิดอยู่
            if execute_on:
                manage.manage_positions(cfg, bal)

            # 2.6) journal ไม้ที่ปิด + รายงานทุกไม้ที่เพิ่งปิด + เรียนรู้ (ทุก ~60 วิ)
            if now - last_journal > 60:
                try:
                    _meta = _load_trade_src()   # โหลดครั้งเดียวต่อรอบ journal
                    for d in journal.record_closed():
                        emo = "✅ กำไร" if d["profit"] >= 0 else "🔴 ขาดทุน"
                        pct = (f" = {d['profit'] / bal * 100:+.2f}% ของทุน ${bal:,.0f}"
                               if bal and bal > 0 else "")
                        day = journal.today_pnl()
                        # หาชื่อเทคนิคจาก position_id → trade meta
                        _src = _meta.get(str(d.get("position_id", "")), "")
                        _src_label = _SRC_MAP.get(_src, "")
                        src_line = f"\n⚙️ {_src_label}" if _src_label else ""
                        tg.send_text(token, chat,
                                     f"{emo} · ปิดไม้ {d['symbol']} {d['volume']} lot{src_line}\n"
                                     f"💰 P/L ${d['profit']:+.2f}{pct}\n"
                                     f"📊 รวมวันนี้ ${day:+.2f}")
                    learn.attach_outcomes()   # จับคู่ผลลัพธ์เข้าฟีเจอร์ (closed-loop เรียนรู้)
                except Exception:  # noqa: BLE001
                    pass
                last_journal = now

            # 2.65) สรุปประจำวัน + เช็กสินทรัพย์ใหม่ที่โบรกเพิ่ม (ครั้งเดียว/วัน)
            _today = datetime.now().strftime("%Y-%m-%d")
            if _state.get("last_digest") != _today and datetime.now().hour >= digest_hour:
                try:
                    tg.send_text(token, chat, _daily_digest_text())
                    cur = set(m.list_symbols())                    # เช็ก symbol ใหม่
                    known = set(_state.get("known_syms", []))
                    new = cur - known
                    if known and new:
                        tg.send_text(token, chat, f"🆕 โบรกเพิ่มสินทรัพย์ใหม่ {len(new)} ตัว:\n"
                                     + ", ".join(sorted(new)[:30])
                                     + "\nบอทจะพิจารณาเทรดถ้าผ่านเทคนิค — บอกผมถ้าอยากเพิ่มเข้า watchlist")
                    _state["known_syms"] = sorted(cur)
                except Exception:  # noqa: BLE001
                    pass
                _state["last_digest"] = _today
                _save_state(_state)

            # 2.7) เบรกขาดทุนต่อวัน
            if bal > 0:
                dpnl = journal.today_pnl()
                if dpnl <= -(bal * max_daily_loss / 100.0):
                    if not daily_halt:
                        tg.send_text(token, chat, f"🛑 หยุดเทรดวันนี้ — ขาดทุน ${dpnl:.2f} ถึงเพดาน {max_daily_loss}% (จัดการไม้เก่าต่อ แต่ไม่เปิดใหม่)")
                        daily_halt = True
                elif daily_halt and dpnl > -(bal * max_daily_loss / 200.0):
                    daily_halt = False  # ฟื้นเมื่อขาดทุนลดลงครึ่งเพดาน

            # 3) หาโอกาสใหม่ (ถ้าไม่ halt · ไม่ pause · ไม่ใช่ช่วงข่าวแรง)
            open_n = _count_open() if auto_on else 0
            if auto_on and open_n < prev_open:        # มีไม้เพิ่งปิด (TP/SL) → สแกนหาตัวใหม่ทันที
                last_scan = 0.0
                log.info("มีไม้ปิด เหลือ %d ไม้ → สแกนหาตัวใหม่ทันที (ไม่รอครบรอบ)", open_n)
            prev_open = open_n
            blocked = daily_halt or _is_paused()
            slot_free = (open_n < max_pos) if auto_on else (pending is None)
            if slot_free and not blocked:
                blackout, ev = news_guard.is_blackout(finnhub, blackout_min)
                if blackout:
                    if now - last_scan > scan_gap:   # log เป็นระยะ ไม่สแกนช่วงข่าว
                        log.info("งดเปิดไม้ — ใกล้ข่าวแรง: %s", ev)
                        last_scan = now
                else:
                    if not queue and now - last_scan > scan_gap:
                        syms = set(m.list_symbols())
                        queue = []
                        if use_supertrend:                 # SuperTrend H1
                            queue += _scan_supertrend(cfg, syms)
                        if use_halftrend:                  # HalfTrend H1
                            queue += _scan_halftrend(cfg, syms)
                        if use_utbot and auto_on:          # UT Bot M15 (ตอบสนองไว)
                            queue += _scan_utbot(cfg, syms)
                        if use_ema_stoch and auto_on:      # EMA+Stoch M15
                            queue += _scan_scalp(cfg, syms)
                        if use_fx_orb and auto_on:         # FX ORB London
                            queue += _scan_fx_orb(cfg, syms)
                        if use_hybrid and auto_on:         # Hybrid-Pro H1+M15
                            queue += _scan_hybrid(cfg, syms)
                        last_scan = now
                        log.info("สแกนได้ %d ตัวมีทิศ", len(queue))
                    if auto_on:
                        # โหมด Auto: ยิงเองทันทีจนเต็มเพดานไม้ (ไม่ต้องกดปุ่ม)
                        opened = _open_symbols()
                        scan_res = []     # (symbol, dir, status, detail) → สรุปส่ง Telegram
                        while queue and _count_open() < max_pos:
                            bias, hint = queue.pop(0)
                            sym, dr = bias["symbol"], bias["direction"]
                            if sym in opened:                     # ถืออยู่แล้ว → ข้ามเงียบ
                                continue
                            key = sym + dr
                            if now - recent.get(key, 0) < cooldown:
                                continue
                            # crypto blackout เพิ่มเติม (ช่วงบาง + options expiry)
                            import market_hours   # ต้อง import ก่อนใช้ครั้งแรก (กัน UnboundLocalError จาก Python scoping)
                            if market_hours.category(sym) == "crypto":
                                c_blk, c_ev = news_guard.is_blackout_crypto(finnhub, blackout_min)
                                if c_blk:
                                    scan_res.append((sym, dr, "skip", f"crypto blackout: {c_ev}"))
                                    log.info("⛔ ข้าม %s — crypto blackout: %s", sym, c_ev)
                                    continue
                            if max_per_dir > 0 and _count_direction(dr) >= max_per_dir:
                                scan_res.append((sym, dr, "skip", f"ทิศ {dr} เต็ม ({max_per_dir} ไม้)"))
                                continue
                            if datetime.now().weekday() < 5:       # กระจายเสี่ยง (จ-ศ · ลิมิตแยกตามกลุ่ม)
                                grp = market_hours.correlation_group(sym)
                                lim = max_per_grp_us if grp == "หุ้น/ดัชนี" else max_per_grp
                                if lim > 0 and _count_group(grp) >= lim:
                                    scan_res.append((sym, dr, "skip", f"กระจายเสี่ยง: {grp} ครบ {lim}"))
                                    continue
                            t = tk.build_ticket(sym, bias, acc_now, cfg, m, part1_hint=hint,
                                                scalp=bias.get("scalp"))
                            if not t:
                                continue
                            if t.get("skipped"):                  # ติด guardrail ความเสี่ยง
                                scan_res.append((sym, dr, "skip", t.get("reason", "ข้าม")))
                                continue
                            dec = t["verdict"].get("decision")
                            conf = t["verdict"].get("confidence") or 0
                            if dec not in accept:
                                # AI บอก skip/manual → เชื่อ AI ข้ามทันที (ไม่มีโหมด CDC นำแล้ว)
                                scan_res.append((sym, dr, "skip", f"AI {dec} {conf}%"))
                                log.info("⛔ ข้าม %s %s — AI: %s %s%% %s", sym, dr, dec, conf,
                                         (t["verdict"].get("reason") or "")[:60])
                                recent[key] = now + scan_gap   # กัน re-scan รอบถัดมาทันที
                                continue
                            recent[key] = now
                            if _auto_open(cfg, token, chat, t, execute_on):
                                opened.add(sym)
                                scan_res.append((sym, dr, "open", _strat_tags(t)))
                        if scan_res and notify_scan:              # ส่งสรุปสแกน (ไม่ส่งซ้ำถ้าผลเหมือนเดิม)
                            kset = frozenset((s, st) for s, _, st, _ in scan_res)
                            if kset != last_scan_key:
                                tg.send_text(token, chat, _scan_summary(scan_res))
                                last_scan_key = kset
                    else:
                        # โหมด Manual: เสนอใบ + ปุ่มทีละใบ
                        opened = _open_symbols()   # ตรวจไม้ที่ถืออยู่ — กันเสนอซ้ำตัวเดิม
                        while queue and pending is None:
                            bias, hint = queue.pop(0)
                            if bias["symbol"] in opened:   # ถืออยู่แล้ว → ข้ามเงียบ
                                continue
                            key = bias["symbol"] + bias["direction"]
                            if now - recent.get(key, 0) < cooldown:
                                continue
                            t = tk.build_ticket(bias["symbol"], bias, acc_now, cfg, m, part1_hint=hint)
                            if not t or t.get("skipped") or t["verdict"].get("decision") not in accept:
                                continue
                            tid = f"t{counter}"; counter += 1
                            text = tk.format_ticket(t) + f"\n\n⏳ ตอบใน {ttl // 60} นาที ไม่งั้นใบสั่งจะหายไป"
                            mid = tg.send_ticket(token, chat, text, tid)
                            if mid:
                                pending = {"tid": tid, "msg_id": mid, "ticket": t, "sent_at": now}
                                recent[key] = now
                                log.info("เสนอใบสั่ง %s %s", t["exsym"], t["direction"])
            time.sleep(2)
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            log.exception("loop error: %s", e)
            time.sleep(5)
    m.shutdown()


if __name__ == "__main__":
    main()
