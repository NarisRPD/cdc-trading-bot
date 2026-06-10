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
import market_hours
from run import _watchlist

# ── Logging: Python จัดการ rotation เอง (เก็บ log ย้อนหลัง N วัน · เก่ากว่าลบอัตโนมัติ) ──
# ตัดทุกเที่ยงคืน → part2.log (วันนี้) + part2.log.YYYY-MM-DD (N วันก่อน)
# จำนวนวันปรับได้ผ่าน env PART2_LOG_DAYS (ดีฟอลต์ 7) — เพิ่มถ้าอยากย้อนดูเหตุการณ์เก่า
# *** .bat ต้องรัน `python interactive.py` เฉย ๆ (ห้าม >> part2.log 2>&1) ไม่งั้นเขียนซ้ำ/rotate พัง ***
from logging.handlers import TimedRotatingFileHandler
# path ของ log ตั้งค่าได้ผ่าน env PART2_LOG_DIR → ชี้ไปโฟลเดอร์ OneDrive/Drive
# เพื่อให้ cloud sync ไฟล์ขึ้นอัตโนมัติ (เปิดดูจากมือถือ/คอมบ้านได้สด) · ดีฟอลต์ = โฟลเดอร์ script
_LOG_DIR = os.getenv("PART2_LOG_DIR", "").strip() or os.path.dirname(os.path.abspath(__file__))
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
except OSError:
    _LOG_DIR = os.path.dirname(os.path.abspath(__file__))   # path เสีย → fallback โฟลเดอร์ script
_LOG_FILE = os.path.join(_LOG_DIR, "part2.log")
try:
    _LOG_DAYS = int(os.getenv("PART2_LOG_DAYS", "7") or "7")
except ValueError:
    _LOG_DAYS = 7
_log_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
_file_handler = TimedRotatingFileHandler(_LOG_FILE, when="midnight", interval=1,
                                         backupCount=_LOG_DAYS, encoding="utf-8")
_file_handler.setFormatter(_log_fmt)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_fmt)
# *** สำคัญ: `from run import _watchlist` ด้านบนรัน run.py ซึ่งเรียก logging.basicConfig() ตอน import ***
# → root logger มี handler (console) แล้ว ทำให้ basicConfig() ตรงนี้กลายเป็น no-op
#   = file handler ไม่ติด → part2.log ว่างเปล่า (เคยเป็นบั๊กนี้)
# แก้: ผูก handler เองโดยตรง (idempotent ไม่ว่า import order ไหน) — ล้างของเดิมก่อนกัน log ซ้ำ
_root = logging.getLogger()
for _h in list(_root.handlers):
    _root.removeHandler(_h)
_root.setLevel(logging.INFO)
_root.addHandler(_file_handler)
_root.addHandler(_console_handler)
log = logging.getLogger("part2.interactive")

# จับ exception ที่หลุด (uncaught) → เขียนลง part2.log ด้วย (เดิมไปแค่ console → หาย ดู crash ไม่ได้)
# กันเคสบอท crash แล้ว part2.log ไม่มีร่องรอยว่าพังเพราะอะไร
def _log_uncaught(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):        # Ctrl+C → ปล่อยให้ออกปกติ
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    log.critical("บอท crash (uncaught exception): %s", exc_value,
                 exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = _log_uncaught

TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# ── path ของ config.env (อยู่ในโฟลเดอร์เดียวกับ script นี้) ─────────────────
_CFG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")

# keys ที่ห้ามแสดงหรือแก้ผ่าน /set และ /ai (ป้องกัน secret รั่ว)
_SECRET_KEYS = {"MT5_PASSWORD", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                "GEMINI_API_KEY", "GEMINI_API_KEY_2", "FINNHUB_API_KEY"}


def _cfg_write(key: str, value: str) -> bool:
    """เขียน KEY=VALUE ลง config.env — update ถ้ามีแล้ว, append ถ้าไม่มี
    ป้องกัน: ไม่แตะ secret keys, backup อัตโนมัติ"""
    import re
    key = key.strip().upper()
    if key in _SECRET_KEYS:
        log.warning("_cfg_write: ห้ามแก้ secret key %s ผ่าน Telegram", key)
        return False
    try:
        with open(_CFG_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        # backup ก่อนแก้
        with open(_CFG_FILE + ".bak", "w", encoding="utf-8") as f:
            f.write(content)
        pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
        if pattern.search(content):
            new_content = pattern.sub(f"{key}={value}", content)
        else:
            new_content = content.rstrip("\n") + f"\n{key}={value}\n"
        # atomic write: เขียน .tmp ก่อน แล้ว replace — crash กลางทางไม่ทำให้ config เสีย
        _tmp = _CFG_FILE + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(_tmp, _CFG_FILE)   # atomic บน Windows (same drive)
        log.info("cfg_write: %s=%s", key, value)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("_cfg_write failed: %s", e)
        return False


def _cfg_snapshot(cfg: dict) -> str:
    """คืน config สำหรับส่งหา Gemini (ซ่อน secret, max 50 lines)"""
    lines = []
    for k, v in sorted(cfg.items()):
        if k in _SECRET_KEYS:
            continue
        lines.append(f"{k}={v}")
    return "\n".join(lines[:50])


def _ai_cfg_edit(request: str, cfg: dict) -> list:
    """ส่งคำขอภาษาธรรมชาติไป Gemini → รับ list ของ {key, value, reason}
    ใช้ REST ตรง (ไม่ผ่าน gemini_gate.assess ซึ่ง schema ต่างกัน)"""
    import json as _json, requests as _req
    api_key = cfg.get("GEMINI_API_KEY", "")
    if not api_key:
        return []
    snap = _cfg_snapshot(cfg)
    prompt = f"""คุณเป็น config editor ของ MT5 trading bot ที่รันอยู่บน Windows VPS

คำขอ: "{request}"

config ปัจจุบัน (ซ่อน secret):
{snap}

ตอบ JSON array เท่านั้น — ห้ามมีข้อความอื่น:
[{{"key": "KEY_NAME", "value": "new_value", "reason": "เหตุผลสั้นๆ ภาษาไทย"}}]

กฎ:
- boolean ใช้ "true" หรือ "false" เท่านั้น
- ตัวเลขส่งเป็น string เช่น "1.5"
- ถ้าไม่แน่ใจว่า key ไหน หรือคำขอไม่ชัดเจน → return []
- ห้ามแก้ key ที่เป็น password/token/secret"""
    try:
        model = "gemini-2.0-flash"
        url   = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body  = {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0.1}}
        r = _req.post(url, params={"key": api_key}, json=body, timeout=30)
        if r.status_code != 200:
            log.warning("_ai_cfg_edit Gemini error: %s", r.text[:120])
            return []
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        # ตัด markdown code block ออกถ้ามี
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:]).rstrip("`").strip()
        return _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("_ai_cfg_edit failed: %s", e)
        return []

# ── ชื่อเทคนิคสำหรับแสดงใน Telegram ────────────────────────────────
_SRC_MAP = {
    "supertrend": "📈 SuperTrend",
    "halftrend":  "〰️ HalfTrend",
    "utbot":      "🤖 UT Bot",
    "hybrid":     "🔀 Hybrid-Pro",
    "scalp":      "⚡ EMA+Stoch",
    "ema_m5":     "🎯 EMA Ribbon M5",
    "fx_orb":     "🌅 FX ORB",
    "pa":         "📐 Price Action",
    "vwap":       "🌊 VWAP Bounce",
    "bb_squeeze": "🎸 BB Squeeze",
    "rsi_div":    "📉 RSI Divergence",
    "orb_pro":    "🕐 ORB Session",
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
    import pandas as pd
    tf = cfg.get("UTB_TF", "M15")
    kv = float(cfg.get("UTB_KEY_VALUE", "1.0"))
    ap = int(cfg.get("UTB_ATR_PERIOD", "10"))
    fresh = int(cfg.get("UTB_FRESH_BARS", "2"))
    rr = float(cfg.get("UTB_RR", "1.8"))
    stale_min = 45
    out = []
    h1_conflicts = 0       # นับตัวที่ M15 ขัด H1 — log สรุปบรรทัดเดียวแทนรายตัว
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
                        log.debug("ข้าม %s — UT Bot(%s) %s ขัด H1 SuperTrend (%s)",
                                  sym, tf, sig["direction"], _h1_dir)
                        h1_conflicts += 1
                        continue

        out.append(({"symbol": sym, "direction": sig["direction"], "source": "utbot",
                     "st_value": sig.get("ts_value"), "rsi": None,
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"UT Bot {tf} (kv={kv})"}}, None))
    if out or h1_conflicts:
        log.info("utbot(%s): เจอ %d สัญญาณ · ข้าม %d ขัด H1 SuperTrend", tf, len(out), h1_conflicts)
    return out


def _scan_scalp(cfg, broker: set) -> list:
    """สแกน EMA+Stoch บน SCALP_TF (default M15 · เฉพาะของผันผวน เลี่ยง FX ตามผล backtest)
    เติมจังหวะ 'ตามเทรนด์' ระหว่างรอสัญญาณหลัก — ยังต้องผ่านเกราะ build_ticket ทุกด่าน
    bias.scalp = {sl, rr, tag} → build_ticket ใช้ SL/TP ของกลยุทธ์เอง (ปิดไว ไม่โดนกฎ +2%)
    ⚠️ TF ต่ำลง = ระยะ TP หดตาม √เวลา แต่ spread คงที่ → ดู R:R หลังหักต้นทุนใน log"""
    import scalp as _scalp
    import pandas as pd
    tf = cfg.get("SCALP_TF", "M15")
    rr = float(cfg.get("SCALP_RR", "1.8"))
    wk = (datetime.now(timezone.utc).weekday() >= 5 and
          cfg.get("SCALP_WEEKEND_LOOSEN", "true").lower() in ("1", "true", "yes", "on"))
    os_lvl, ob_lvl = (30.0, 70.0) if wk else (20.0, 80.0)   # เสาร์-อาทิตย์: ผ่อน Stoch ให้ไวขึ้น
    out = []
    for sym in _watchlist(cfg, broker):
        if market_hours.category(sym) == "fx":          # FX ขาดทุนใน backtest → เลี่ยง
            continue
        df = m.rates(sym, tf, 260)
        if df is None or len(df) < 210 or "time" not in df.columns:
            continue
        try:                                             # ไม่มีแท่งใหม่ = ตลาดปิด → ข้าม
            # แปลง last_t ให้เป็น aware datetime (UTC) เพื่อเทียบกับ now(timezone.utc) ได้ถูกต้อง
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            _stale = 20 if tf.upper() in ("M1", "M5") else 45   # TF เร็ว สัญญาณบูดเร็ว
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > _stale:
                continue
        except Exception:  # noqa: BLE001
            pass
        sl_mult = float(cfg.get("SCALP_SL_ATR_MULT", "0.6"))
        sig = _scalp.ema_ribbon_stoch(df, oversold=os_lvl, overbought=ob_lvl, sl_atr_mult=sl_mult)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "zone": None, "rsi": None,
                     "source": "scalp",
                     "scalp": {"sl": sig["sl"], "rr": rr, "tag": f"EMA+Stoch {tf}"}}, None))
    if out:
        log.info("scalp(EMA+Stoch %s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_rvol_brk(cfg, broker: set) -> list:
    """RVOL Spurt + Breakout — จับสินทรัพย์ "In-Play" (volume ผิดปกติ = มี catalyst)
    แล้วเข้าเฉพาะแท่งแรกที่เบรค high/low สำคัญ (เกราะอยู่ใน rvol.signal)
    SL ใต้แท่งเบรค · TP = rr × ระยะ SL · regime filter ยกเว้นให้ (volume spurt
    คือหลักฐานว่า regime เพิ่งเปลี่ยน — H1 ADX ยัง lag ตามไม่ทัน)"""
    import rvol as _rvol
    import pandas as pd
    tf = cfg.get("RVOL_TF", "M15")
    rr = float(cfg.get("RVOL_RR", "1.8"))
    baseline_days = int(cfg.get("RVOL_BASELINE_DAYS", "5") or "5")
    # แท่ง/วัน ตาม TF — ใช้คำนวณจำนวนแท่งที่ต้องดึงให้ครอบ baseline ครบทุกวัน
    _bars_per_day = {"M1": 1440, "M5": 288, "M15": 96, "M30": 48, "H1": 24}
    bpd = _bars_per_day.get(tf.upper(), 96)
    bars_need = max(bpd * (baseline_days + 1), 500)
    out = []
    for sym in _watchlist(cfg, broker):
        if market_hours.category(sym) == "fx":           # นโยบายเดียวกับ scanner อื่น
            continue
        df = m.rates(sym, tf, bars_need)
        if df is None or len(df) < 200 or "time" not in df.columns:
            continue
        try:                                             # breakout ไวต่อเวลา — stale 20 นาทีพอ
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > 20:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _rvol.signal(
            df,
            window_bars=int(cfg.get("RVOL_WINDOW_BARS", "4") or "4"),
            baseline_days=baseline_days,
            rvol_min=float(cfg.get("RVOL_MIN", "2.0") or "2.0"),
            break_bars=int(cfg.get("RVOL_BREAK_BARS", "32") or "32"),
            body_min=float(cfg.get("RVOL_BODY_MIN", "0.5") or "0.5"),
        )
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "zone": None, "rsi": None,
                     "source": "rvol_brk",
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"RVOL Brk {tf} — {sig['reason']}"}}, None))
    if out:
        log.info("rvol_brk(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_range_mr(cfg, broker: set) -> list:
    """สแกน Range-Edge Mean Reversion — เข้าเฉพาะขอบกรอบ sideways ที่กว้างพอ
    (เกราะอยู่ใน range_mr.signal: กรอบ ≥ N×ATR · ADX ต่ำ · แตะขอบซ้ำ · rejection)
    SL/TP สัมบูรณ์ของกลยุทธ์ (SL นอกขอบ · TP กลางกรอบ) ส่งผ่าน bias.scalp
    หมายเหตุ: source "range_mr" อยู่ใน _MEAN_REVERSION_SRC → regime filter ยกเว้นให้"""
    import range_mr as _rmr
    import pandas as pd
    tf = cfg.get("RANGE_MR_TF", "M15")
    lookback = int(cfg.get("RANGE_MR_BARS", "48") or "48")
    out = []
    for sym in _watchlist(cfg, broker):
        if market_hours.category(sym) == "fx":           # FX ปิดผ่าน TRADE_FX อยู่แล้ว — ข้ามตั้งแต่สแกน
            continue
        df = m.rates(sym, tf, max(lookback + 80, 260))
        if df is None or len(df) < lookback + 60 or "time" not in df.columns:
            continue
        try:                                             # ไม่มีแท่งใหม่ = ตลาดปิด → ข้าม
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > 45:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _rmr.signal(
            df,
            lookback=lookback,
            min_width_atr=float(cfg.get("RANGE_MR_MIN_WIDTH_ATR", "3.0") or "3.0"),
            edge_pct=float(cfg.get("RANGE_MR_EDGE_PCT", "0.10") or "0.10"),
            min_touches=int(cfg.get("RANGE_MR_MIN_TOUCHES", "2") or "2"),
            sl_atr=float(cfg.get("RANGE_MR_SL_ATR", "0.3") or "0.3"),
            chop_min=float(cfg.get("RANGE_MR_CHOP_MIN", "50") or "50"),
            max_width_atr=float(cfg.get("RANGE_MR_MAX_WIDTH_ATR", "6.0") or "6.0"),
        )
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "zone": None, "rsi": None,
                     "source": "range_mr",
                     "scalp": {"sl": sig["sl"], "tp": sig["tp"],
                               "tag": f"Range MR {tf} — {sig['reason']}"}}, None))
    if out:
        log.info("range_mr(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_fx_orb(cfg, broker: set) -> list:
    """สแกน Asian-London ORB เฉพาะคู่เงิน (รันเฉพาะหน้าต่าง London 07-11 UTC) → [(bias, None)]
    TP/SL สัมบูรณ์ของกลยุทธ์ (TP=ความกว้างกรอบ · SL=กึ่งกลาง) ส่งผ่าน bias.scalp"""
    if not (7 <= datetime.now(timezone.utc).hour < 11):   # นอกหน้าต่าง London → ไม่ต้องสแกน
        return []
    import scalp as _scalp
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


def _scan_ema_m5(cfg, broker: set) -> list:
    """#6 EMA Ribbon Scalp (M5) — กลยุทธ์ scalp 5-10 นาที สำหรับ FX คู่ volume สูง
    ใช้โดย prop firms ระดับโลก (SMB Capital, T3 Trading): EMA 8/21 cross + RSI momentum zone
    - สแกนเฉพาะ FX pairs ที่ระบุใน FX_SCALP_SYMBOLS (default: major pairs)
    - TF: M5 (default) หรือ M1 ตาม EMA_M5_TF
    - เข้าเมื่อ EMA fast ตัด slow + RSI อยู่ใน momentum zone (40-65)
    - SL: 1×ATR(M5) ใต้/เหนือ EMA slow — แน่นเหมาะ scalp ออกไวถ้ากราฟเสียทรง"""
    import scalp as _scalp
    import pandas as pd
    tf      = cfg.get("EMA_M5_TF",    "M5")
    fast    = int(cfg.get("EMA_FAST",  "8"))
    slow    = int(cfg.get("EMA_SLOW", "21"))
    rsi_min = float(cfg.get("EMA_RSI_MIN", "40"))
    rsi_max = float(cfg.get("EMA_RSI_MAX", "65"))
    fresh   = int(cfg.get("EMA_FRESH_BARS", "3"))
    sl_mult = float(cfg.get("EMA_SL_ATR",  "1.0"))
    rr      = float(cfg.get("EMA_M5_RR",   "2.0"))

    # FX pairs ที่จะสแกน — default: 4 major pairs volume สูงสุด
    _raw_syms = cfg.get("FX_SCALP_SYMBOLS", "EURUSD,GBPUSD,USDJPY,AUDUSD").strip()
    _want_fx = {s.strip() for s in _raw_syms.split(",") if s.strip()}

    stale_min = 20   # M5 bar stale กว่า 20 นาที = ตลาดปิดหรือ off-hours → ข้าม
    out = []
    from symbol_map import resolve
    for core_sym in _want_fx:
        sym = resolve(core_sym, broker)
        if not sym:
            continue
        # ตรวจว่าเป็น FX จริง
        if market_hours.category(sym) != "fx":
            continue
        df = m.rates(sym, tf, 200)
        if df is None or len(df) < slow + 20 or "time" not in df.columns:
            continue
        try:
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _scalp.ema_ribbon_signal(df, fast=fast, slow=slow,
                                       rsi_min=rsi_min, rsi_max=rsi_max,
                                       fresh_bars=fresh, sl_atr_mult=sl_mult)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "source": "ema_m5",
                     "st_value": sig.get("ema_slow"), "rsi": sig.get("rsi"),
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"EMA {fast}/{slow} {tf}"}}, None))
    if out:
        log.info("ema_m5(%s): เจอ %d สัญญาณ FX", tf, len(out))
    return out


def _scan_vwap(cfg, broker: set) -> list:
    """VWAP Bounce scanner — สแกน watchlist ทั้งหมด บน M5 (ต้องการ 300 bars = ข้อมูลต้นวัน)
    ราคา pullback มาแตะ VWAP แล้วเด้งออก = สถาบันรับ/ขายที่ราคายุติธรรม"""
    import scalp as _scalp, pandas as pd
    tf       = cfg.get("VWAP_TF",      "M5")
    std_ent  = float(cfg.get("VWAP_STD_ENTRY", "0.5"))
    std_sl   = float(cfg.get("VWAP_STD_SL",    "1.5"))
    rsi_min  = float(cfg.get("VWAP_RSI_MIN",   "35"))
    rsi_max  = float(cfg.get("VWAP_RSI_MAX",   "65"))
    rr       = float(cfg.get("VWAP_RR",        "2.0"))
    stale_min = 20 if tf in ("M1", "M5") else 60
    out = []
    for sym in _watchlist(cfg, broker):
        df = m.rates(sym, tf, 300)     # 300 bars M5 = ~25 ชั่วโมง (ครอบ VWAP ต้นวัน)
        if df is None or len(df) < 30 or "time" not in df.columns:
            continue
        try:
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _scalp.vwap_bounce_signal(df, std_entry=std_ent, std_sl=std_sl,
                                        rsi_min=rsi_min, rsi_max=rsi_max)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "source": "vwap",
                     "st_value": sig.get("vwap"), "rsi": sig.get("rsi"),
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"VWAP Bounce {tf}"}}, None))
    if out:
        log.info("VWAP(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_bb_squeeze(cfg, broker: set) -> list:
    """BB Squeeze Breakout scanner — สแกน watchlist ทั้งหมด บน M5
    BB แคบลง (ตลาดนิ่ง) → จับ momentum burst ที่ออกมา + volume ยืนยัน"""
    import scalp as _scalp, pandas as pd
    tf         = cfg.get("BB_TF",             "M5")
    bb_period  = int(cfg.get("BB_PERIOD",      "20"))
    bb_std     = float(cfg.get("BB_STD",       "2.0"))
    bb_sq_lb   = int(cfg.get("BB_SQUEEZE_LOOKBACK", "50"))
    vol_mult   = float(cfg.get("BB_VOL_MULT",  "1.2"))
    rr         = float(cfg.get("BB_RR",        "2.0"))
    stale_min  = 20 if tf in ("M1", "M5") else 60
    need_bars  = bb_period + bb_sq_lb + 10
    out = []
    for sym in _watchlist(cfg, broker):
        df = m.rates(sym, tf, need_bars + 20)
        if df is None or len(df) < need_bars or "time" not in df.columns:
            continue
        try:
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _scalp.bb_squeeze_signal(df, bb_period=bb_period, bb_std_mult=bb_std,
                                       squeeze_lookback=bb_sq_lb, volume_min_mult=vol_mult)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "source": "bb_squeeze",
                     "st_value": sig.get("bb_mid"), "rsi": None,
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"BB Squeeze {tf}"}}, None))
    if out:
        log.info("BB Squeeze(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_rsi_div(cfg, broker: set) -> list:
    """RSI Divergence scanner — สแกน watchlist ทั้งหมด บน M5
    ราคาไปต่อแต่ momentum ลดลง = จุดกลับตัวระยะสั้น"""
    import scalp as _scalp, pandas as pd
    tf         = cfg.get("RSI_DIV_TF",       "M5")
    rsi_period = int(cfg.get("RSI_DIV_PERIOD",  "14"))
    lookback   = int(cfg.get("RSI_DIV_LOOKBACK", "30"))
    swing_str  = int(cfg.get("RSI_DIV_SWING",    "2"))
    rr         = float(cfg.get("RSI_DIV_RR",    "2.0"))
    stale_min  = 20 if tf in ("M1", "M5") else 60
    need_bars  = rsi_period + lookback + 15
    out = []
    for sym in _watchlist(cfg, broker):
        df = m.rates(sym, tf, need_bars + 20)
        if df is None or len(df) < need_bars or "time" not in df.columns:
            continue
        try:
            last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                continue
        except Exception:  # noqa: BLE001
            pass
        sig = _scalp.rsi_divergence_signal(df, rsi_period=rsi_period,
                                           lookback=lookback, swing_strength=swing_str)
        if not sig.get("detected"):
            continue
        out.append(({"symbol": sym, "direction": sig["direction"], "source": "rsi_div",
                     "st_value": None, "rsi": sig.get("rsi"),
                     "scalp": {"sl": sig["sl"], "rr": rr,
                               "tag": f"RSI {sig.get('div_type','div')} {tf}"}}, None))
    if out:
        log.info("RSI Div(%s): เจอ %d สัญญาณ", tf, len(out))
    return out


def _scan_orb_pro(cfg, broker: set) -> list:
    """Opening Range Breakout (Toby Crabel)
    session → ประเภทสินทรัพย์: london/ny = FX pairs · us = ดัชนี US (US30/USTEC/US500)
    ORB_SESSION: comma-separated เช่น "london,us" · "both" = london,ny (backward compat)
    หมายเหตุ us: build_ticket ยกเว้น volatile-window guard ให้ source orb_pro —
    ORB คือกลยุทธ์เดียวที่ออกแบบมาเล่นช่วง open โดยเฉพาะ (SL สั้นใต้กรอบ + window จำกัด)"""
    import scalp as _scalp, market_hours, pandas as pd
    sessions_raw = cfg.get("ORB_SESSION", "london").lower().strip()
    if sessions_raw == "both":
        sessions = ["london", "ny"]
    else:
        sessions = [s.strip() for s in sessions_raw.split(",") if s.strip()]
    range_bars   = int(cfg.get("ORB_RANGE_BARS",  "3"))
    window_min   = int(cfg.get("ORB_WINDOW_MIN",  "90"))
    rr           = float(cfg.get("ORB_RR",        "1.5"))
    stale_min    = 20
    out = []
    for session in sessions:
        # us session = ดัชนี US (เปิด 13:30 UTC ชัดเจน gap น้อยกว่าหุ้นเดี่ยว) · อื่นๆ = FX
        want_cat = "us_index" if session == "us" else "fx"
        for sym in _watchlist(cfg, broker):
            if market_hours.category(sym) != want_cat:
                continue
            df = m.rates(sym, "M5", 200)
            if df is None or len(df) < 30 or "time" not in df.columns:
                continue
            try:
                last_t = pd.to_datetime(df["time"].iloc[-1]).to_pydatetime().replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last_t).total_seconds() / 60.0 > stale_min:
                    continue
            except Exception:  # noqa: BLE001
                pass
            sig = _scalp.orb_session_signal(df, session=session,
                                            range_bars=range_bars, trade_window_min=window_min)
            if not sig.get("detected"):
                continue
            out.append(({"symbol": sym, "direction": sig["direction"], "source": "orb_pro",
                         "st_value": None, "rsi": None,
                         "scalp": {"sl": sig["sl"], "rr": rr,
                                   "tag": f"ORB {session.upper()}"}}, None))
    if out:
        log.info("ORB Pro: เจอ %d สัญญาณ", len(out))
    return out


def _scan_pa(cfg, broker: set) -> list:
    """Price Action & Market Structure Scanner — H1 (default)

    2 รูปแบบสัญญาณ:
    1. Reversal at Key S/R — uptrend→ซื้อที่แนวรับ, downtrend→ขายที่แนวต้าน + แท่งกลับตัวยืนยัน
    2. Breakout & Retest (BRT) — เบรกแนว → ย่อกลับมาทดสอบ → แท่งยืนยัน → เข้าตามเทรนด์

    ใช้ฟังก์ชันที่มีอยู่แล้ว ไม่สร้างโค้ดซ้ำ:
      patterns.structure()          → โครงสร้าง HH/HL หรือ LH/LL
      patterns.support_resistance() → หาแนวรับ-ต้านใกล้ราคา
      candles.confirms()            → ยืนยันแท่งกลับตัว
      patterns.breakout_retest()    → ตรวจ Breakout & Retest pattern
    """
    import candles
    import patterns   # ต้อง import ใน function scope — ไม่ได้ import ที่ module level ใน interactive.py
    import pandas as pd
    tf = cfg.get("PA_TF", "H1")
    rr = float(cfg.get("PA_RR", "2.0"))
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

        # คำนวณ ATR (True Range 14 แท่ง — ใช้คู่กับ support_resistance)
        import numpy as _np
        _h = df["high"].astype(float).values
        _l = df["low"].astype(float).values
        _c_arr = df["close"].astype(float).values
        _pc = _np.roll(_c_arr, 1); _pc[0] = _c_arr[0]
        _tr = _np.maximum(_h - _l, _np.maximum(abs(_h - _pc), abs(_l - _pc)))
        atr = float(_np.mean(_tr[-14:]))
        if atr <= 0:
            continue

        # ── วิเคราะห์โครงสร้างตลาด ────────────────────────────────────────────
        struct = patterns.structure(df, lookback=60, swing=3)
        trend = struct.get("trend", "side")   # "up" / "down" / "side"

        # ── หาแนวรับ-ต้านใกล้ราคาปัจจุบัน ─────────────────────────────────────
        sr = patterns.support_resistance(df, lookback=60, swing=3, atr=atr)

        direction = None
        sl = None
        tag = None

        # ── รูปแบบที่ 1: Reversal at Key S/R ─────────────────────────────────────
        # Uptrend (HH/HL) + ราคาใกล้แนวรับ + แท่งกลับตัวขาขึ้น → BUY
        # เหตุผล: ตลาดทำ HH/HL = โครงสร้างขาขึ้น → ย่อมาแนวรับ = จังหวะต่อเทรนด์
        if trend == "up" and sr.get("near_support") and sr.get("support") is not None:
            cdl = candles.confirms(df, "buy")
            if cdl:
                sl = round(sr["support"] - 0.5 * atr, 5)
                direction = "buy"
                tag = f"PA Reversal HL {tf}"

        # Downtrend (LH/LL) + ราคาใกล้แนวต้าน + แท่งกลับตัวขาลง → SELL
        # เหตุผล: ตลาดทำ LH/LL = โครงสร้างขาลง → เด้งมาแนวต้าน = จังหวะต่อเทรนด์
        elif trend == "down" and sr.get("near_resistance") and sr.get("resistance") is not None:
            cdl = candles.confirms(df, "sell")
            if cdl:
                sl = round(sr["resistance"] + 0.5 * atr, 5)
                direction = "sell"
                tag = f"PA Reversal LH {tf}"

        # ── รูปแบบที่ 2: Breakout & Retest ───────────────────────────────────────
        # breakout_retest() ตรวจแท่งกลับตัวไว้ก่อนแล้ว → ไม่ต้องเช็กซ้ำ
        # ใช้เมื่อ Reversal ยังไม่ตรงเงื่อนไข (trend sideways / ราคายังไม่ถึงแนว)
        if direction is None:
            for _d in ("buy", "sell"):
                brt = patterns.breakout_retest(df, direction=_d, atr=atr, lookback=60, swing=3)
                if brt.get("detected"):
                    direction = _d
                    sl = brt["sl"]
                    tag = f"PA Breakout Retest {tf}"
                    break

        if direction is None or sl is None:
            continue

        # ตรวจ SL ถูกฝั่ง + ห่างพอ (กัน SL ผิดด้าน / ชิดราคาจน noise เขี่ย)
        c_price = float(df["close"].iloc[-1])
        side_ok = (sl < c_price) if direction == "buy" else (sl > c_price)
        if not side_ok or abs(c_price - sl) < 0.2 * atr:
            continue

        out.append((
            {"symbol": sym, "direction": direction, "source": "pa",
             "st_value": None, "rsi": None,
             "scalp": {"sl": sl, "rr": rr, "tag": tag}},
            None,
        ))
    if out:
        log.info("pa(%s): เจอ %d สัญญาณ", tf, len(out))
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
    _confl = t.get("confluence") or []
    if t.get("confluence_boosted") and len(_confl) >= 2:
        tags.append(f"🔗×{len(_confl)}")      # หลายกลยุทธ์เห็นพ้อง → lot ใหญ่ขึ้น
    if t.get("reduced"):
        tags.append("ไม้เล็ก")
    for k, lbl in (("three_bar", "3BP"), ("brt", "BRT"), ("ibb", "IBB"), ("tlp", "2Leg")):
        if (t.get(k) or {}).get("detected"):
            tags.append(lbl)
    return " · ".join(tags)


def _annotate_confluence(queue: list) -> None:
    """Confluence (Confirmation mode): นับว่ากี่กลยุทธ์ entry เห็นพ้อง (symbol, direction) เดียวกัน
    → เติม bias['confluence'] = list ของ source ที่ตรงกัน (รวมตัวเอง)

    *** ไม่กรองไม้ทิ้ง *** — ไม้ที่หลายกลยุทธ์ยืนยันแค่ได้ lot/ความมั่นใจมากขึ้น (ดู build_ticket)
    mutate queue ในที่ — bias ทุกตัวจะมี key 'confluence' หลังเรียก"""
    agree: dict = {}
    for bias, _hint in queue:
        k = (bias["symbol"], bias["direction"])
        agree.setdefault(k, set()).add(bias.get("source", ""))
    for bias, _hint in queue:
        k = (bias["symbol"], bias["direction"])
        bias["confluence"] = sorted(s for s in agree.get(k, set()) if s)


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
        # ใช้ position_ticket (ไม่ใช่ order ticket) — บน ECN อาจต่างกัน
        _pos_id = res.get("position_ticket") or res.get("ticket")
        learn.record_entry(_pos_id, t)             # เก็บฟีเจอร์ไว้เรียนรู้ภายหลัง
        # ใช้ position_ticket เป็น key — ตรงกับ position_id ที่ journal.record_closed() ส่งกลับ
        _save_trade_src(_pos_id, (t.get("bias") or {}).get("source", ""))
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
        "/ping — 🏓 เช็คว่าบอทยังมีชีวิต + สถานะ MT5 + ขาดทุนวันนี้\n"
        "/status — สถานะสด: โหมด · พอร์ต · P/L วันนี้ · ไม้ที่เปิด\n"
        "/scan — 🔍 สแกนตลาดทันที (ไม่รอรอบปกติ) + ส่งผลมาที่นี่\n"
        "/stats — สถิติผลเทรดสะสม (win rate · profit factor)\n"
        "/insights — 🧠 บทเรียน: เทคนิคไหนได้เงินจริง (บอทเรียนรู้จากผลจริง)\n"
        "/export [csv|jsonl] — 📊 ส่งออกข้อมูลเทรดไปเทรน AI ภายนอก (ดีฟอลต์ csv)\n"
        "/pause — ⏸️ หยุดเปิดไม้ใหม่ชั่วคราว (ไม้เก่ายังจัดการต่อ)\n"
        "/resume — ▶️ กลับมาเปิดไม้อัตโนมัติ\n"
        "/reset_daily — 🔄 รีเซ็ตโควต้าขาดทุนต่อวัน (นับใหม่จากตอนนี้)\n"
        "/closeall — 🧹 ปิดไม้ Part 2 ทั้งหมดทันที (ฉุกเฉิน)\n"
        "/update — ⬇️ ดึงโค้ดใหม่จาก GitHub แล้ว restart (ใช้ทุกครั้งที่อัปเดต)\n"
        "/stop — 🛑 หยุดบอท (ไม้ที่เปิดอยู่ยังคงเปิดใน MT5)\n"
        "/restart — 🔄 Restart บอท (ไม่ดึงโค้ดใหม่)\n"
        "/help — รายการคำสั่งนี้\n\n"
        "⚙️ แก้ Config ผ่าน Telegram:\n"
        "/set KEY=VALUE — ตั้งค่า config โดยตรง เช่น /set BREAKEVEN_AT_R=2.0\n"
        "/ai <คำขอ> — ให้ AI แก้ config ด้วยภาษาธรรมชาติ\n"
        "  ตัวอย่าง: /ai ปิด HalfTrend\n"
        "  ตัวอย่าง: /ai เพิ่ม breakeven เป็น 2R และปิด notify scan\n"
        "  ⚠️ ต้องพิมพ์ /restart หลังแก้ config ให้มีผล\n\n"
        "🔁 โหมด Auto: บอทสแกน → ตัดสินใจ → ยิงออเดอร์เอง → รายงานที่นี่\n"
        "   เปิดไม้ใหม่เมื่อผ่านด่าน: SuperTrend/HalfTrend/UT Bot/Price Action + แท่งเทียน/วอลุ่ม + Gemini + เกราะความเสี่ยง\n\n"
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
    # Confluence — หลายกลยุทธ์เห็นพ้อง → lot ใหญ่ขึ้น (สัญญาณแข็งแรง)
    _confl = t.get("confluence") or []
    if t.get("confluence_boosted") and len(_confl) >= 2:
        _confl_lbl = " + ".join(_SRC_MAP.get(s, s) for s in _confl)
        lines.append(f"🔗 Confluence ×{len(_confl)}: {_confl_lbl} — เพิ่ม lot")
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
        # ใช้ position_ticket (ไม่ใช่ order ticket) — บน ECN อาจต่างกัน
        _pos_id = res.get("position_ticket") or res.get("ticket")
        learn.record_entry(_pos_id, t)             # เก็บฟีเจอร์ไว้เรียนรู้ภายหลัง
        # ใช้ position_ticket เป็น key — ตรงกับ position_id ที่ journal.record_closed() ส่งกลับ
        _save_trade_src(_pos_id, (t.get("bias") or {}).get("source", ""))
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
    ttl = int(cfg.get("TICKET_TTL_SEC", "180").split("#")[0].strip())
    scan_gap = int(float(cfg.get("SCAN_INTERVAL_MIN", "5")) * 60)
    cooldown = int(cfg.get("SYMBOL_COOLDOWN_SEC", "3600"))
    execute_on = cfg.get("EXECUTE_ORDERS", "false").lower() in ("1", "true", "yes", "on")
    auto_on = cfg.get("AUTO_TRADE", "false").lower() in ("1", "true", "yes", "on")
    use_supertrend = cfg.get("USE_SUPERTREND", "false").lower() in ("1", "true", "yes", "on")   # SuperTrend H1 (opt-in)
    use_halftrend  = cfg.get("USE_HALFTREND",  "false").lower() in ("1", "true", "yes", "on")   # HalfTrend H1 (opt-in)
    use_utbot      = cfg.get("USE_UTBOT",      "false").lower() in ("1", "true", "yes", "on")   # UT Bot M15 (opt-in)
    use_ema_stoch  = cfg.get("USE_EMA_STOCH", "false").lower() in ("1", "true", "yes", "on")   # scalp EMA+Stoch M15
    use_ema_m5     = cfg.get("USE_EMA_M5",   "false").lower() in ("1", "true", "yes", "on")   # EMA Ribbon M5 FX scalp (prop firm strategy)
    use_fx_orb = cfg.get("USE_FX_ORB", "false").lower() in ("1", "true", "yes", "on")        # Asian-London ORB เฉพาะ FX
    use_hybrid = cfg.get("USE_HYBRID_PRO", "false").lower() in ("1", "true", "yes", "on")    # Hybrid-Pro (H1 trend + M15 pullback)
    use_pa        = cfg.get("USE_PA",        "false").lower() in ("1", "true", "yes", "on")  # Price Action & Market Structure H1
    use_vwap      = cfg.get("USE_VWAP",      "false").lower() in ("1", "true", "yes", "on")  # VWAP Bounce M5
    use_bb_squeeze= cfg.get("USE_BB_SQUEEZE","false").lower() in ("1", "true", "yes", "on")  # BB Squeeze Breakout M5
    use_rsi_div   = cfg.get("USE_RSI_DIV",   "false").lower() in ("1", "true", "yes", "on")  # RSI Divergence M5
    use_orb_pro   = cfg.get("USE_ORB_PRO",   "false").lower() in ("1", "true", "yes", "on")  # ORB London/NY session
    use_range_mr  = cfg.get("USE_RANGE_MR",  "false").lower() in ("1", "true", "yes", "on")  # Range-edge mean reversion (ตลาด sideways)
    use_rvol_brk  = cfg.get("USE_RVOL_BRK",  "false").lower() in ("1", "true", "yes", "on")  # RVOL spurt + breakout (in-play momentum)
    max_pos = int(cfg.get("MAX_OPEN_POSITIONS", "5"))
    notify_scan = cfg.get("NOTIFY_SCAN", "false").lower() in ("1", "true", "yes", "on")
    max_dd = float(cfg.get("MAX_DRAWDOWN_PCT", "0") or "0")        # เบรกขาดทุนสะสม (0=ปิด)
    max_per_dir = int(cfg.get("MAX_PER_DIRECTION", "0") or "0")    # จำกัดไม้ทิศเดียวกัน (0=ไม่จำกัด)
    max_per_grp = int(cfg.get("MAX_PER_GROUP", "1") or "0")        # ไม้/กลุ่ม ดีฟอลต์ (จ-ศ · 0=ไม่จำกัด)
    max_per_grp_us = int(cfg.get("MAX_PER_GROUP_US", "2") or "0")  # กลุ่ม "หุ้น/ดัชนี US" เปิดได้กี่ไม้ (US มีหลายตัว)
    fx_session_mode = cfg.get("FX_SESSION_MODE", "false").lower() in ("1", "true", "yes", "on")
    fx_max_pos = int(cfg.get("FX_MAX_POSITIONS", "3") or "3")      # ลิมิตกลุ่ม FX เมื่อ FX_SESSION_MODE เปิด
    digest_hour = int(cfg.get("DIGEST_HOUR", "8"))                # ชั่วโมงส่งสรุปประจำวัน
    heartbeat_sec = int(float(cfg.get("HEARTBEAT_MIN", "0")) * 60)  # แจ้งสถานะทุก N นาที (0=ปิด)
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
    log.info("เริ่ม Part 2 · โหมด=%s · TTL=%ds · สแกนทุก%.1fนาที · maxpos=%d",
             mode_txt, ttl, scan_gap / 60, max_pos)
    tg.send_text(token, chat,
                 f"🤖 Part 2 เริ่มทำงาน · โหมด {mode_txt} · สแกนทุก {scan_gap // 60} นาที\n"
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
    # โหลด daily-halt state จาก _state (persist ผ่าน restart)
    # ถ้า reset_daily ถูกเรียกวันนี้ → ใช้ baseline ที่บันทึกไว้ ไม่ใช่ 0
    _today_iso = datetime.now().date().isoformat()
    _state_baseline_date = _state.get("daily_pnl_baseline_date", "")
    if _state_baseline_date == _today_iso:
        _daily_pnl_baseline = float(_state.get("daily_pnl_baseline", 0) or 0)
        daily_halt = bool(_state.get("daily_halt", False))
        if daily_halt:
            log.info("ดึง daily_halt=True จาก state (วันนี้เคย halt แล้ว)")
    else:
        # วันใหม่ หรือยังไม่เคย reset_daily — เริ่มนับจาก 0
        daily_halt = False
        _daily_pnl_baseline = 0.0
    disconnected = False
    prev_open = 0          # นับไม้เปิด รอบก่อน — ถ้าลดลง = มีไม้ปิด → สแกนหาตัวใหม่ทันที
    last_scan_key = None   # กันส่งสรุปสแกนซ้ำ (ถ้าผลเหมือนเดิม)
    _exc_streak = 0        # นับ loop error ซ้ำต่อเนื่อง — alert Telegram เมื่อซ้ำมาก
    _in_blackout = False   # สถานะ blackout ปัจจุบัน — แจ้ง Telegram แค่ครั้งแรกที่เข้า/ออก
    last_heartbeat = 0.0   # timestamp ส่ง heartbeat ล่าสุด
    _pos_full = False      # สถานะไม้เต็มรอบก่อน — แจ้ง Telegram เมื่อเต็ม/ว่างครั้งแรก
    last_notify_ts = 0.0   # timestamp ส่ง scan summary ล่าสุด — กัน spam ถี่กว่า scan_gap
    _scan_requested = False  # True เมื่อผู้ใช้พิมพ์ /scan → ส่งผลสแกนไป Telegram แม้ notify_scan=false

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
                    # แยก command (word แรก) และ args (ที่เหลือ) อย่างถูกต้อง
                    # ก่อนหน้า: split("@")[0] ไม่ตัด space → cmd รวม args เข้าไปด้วย
                    # ผล: elif cmd == "set" / "ai" ไม่ match → สองคำสั่งนี้ไม่เคยทำงาน
                    _raw_text = (msg.get("text") or "").strip()
                    _parts = _raw_text.split(None, 1)   # split ครั้งเดียวที่ whitespace แรก
                    cmd = _parts[0].lstrip("/").lower().split("@")[0] if _parts else ""
                    args = _parts[1].split() if len(_parts) > 1 else []
                    _args_raw = _parts[1] if len(_parts) > 1 else ""  # รักษา case ของ KEY=VALUE
                    if cmd == "ping":
                        # health check — ตอบทันทีโดยไม่ต้องพึ่ง MT5 หรือ logic อื่น
                        import MetaTrader5 as _m5t
                        _conn = "🟢 MT5 เชื่อมต่อ" if _m5t.terminal_info() else "🔴 MT5 หลุด"
                        _halt_s = "🛑 halt" if daily_halt else ("⏸️ pause" if _is_paused() else "✅ ปกติ")
                        tg.send_text(token, chat,
                                     f"🏓 pong — บอทยังทำงาน\n"
                                     f"{_conn} · สถานะ {_halt_s}\n"
                                     f"ขาดทุนวันนี้: ${journal.today_pnl() - _daily_pnl_baseline:+.2f} "
                                     f"(เพดาน {max_daily_loss}%)")
                    elif cmd in ("help", "start"):
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
                    elif cmd == "export":
                        # /export [csv|jsonl] — ส่งออกข้อมูลเทรน AI เป็นไฟล์เข้า Telegram
                        try:
                            learn.attach_outcomes()        # อัปเดตผลล่าสุดก่อน export
                        except Exception:  # noqa: BLE001
                            pass
                        _fmt = args[0].lower() if args else "csv"
                        if _fmt not in ("csv", "jsonl"):
                            _fmt = "csv"
                        _path = learn.export(_fmt)
                        if not _path:
                            tg.send_text(token, chat,
                                         "📭 ยังไม่มีข้อมูลเทรดให้ export (ต้องมีไม้เปิด/ปิดก่อน)")
                        else:
                            _ov = learn.overview()
                            _cap = (f"📊 ข้อมูลเทรน AI ({_fmt.upper()})\n"
                                    f"ปิดแล้ว {_ov['n']} ไม้ · เปิดอยู่ {_ov['open']} ไม้\n"
                                    "ใช้ feed ML/fine-tune ภายนอกได้")
                            if not tg.send_document(token, chat, _path, _cap):
                                tg.send_text(token, chat, "⚠️ ส่งไฟล์ไม่สำเร็จ — ดู log")
                    elif cmd == "reset_daily":
                        # รีเซ็ตโควต้าขาดทุนต่อวัน — นับใหม่จาก PnL ปัจจุบัน
                        try:
                            _daily_pnl_baseline = journal.today_pnl()
                        except Exception:  # noqa: BLE001
                            _daily_pnl_baseline = 0.0
                        daily_halt = False
                        # บันทึก state ให้ persist ผ่าน restart — ไม่งั้น restart แล้ว halt ติดใหม่
                        _state["daily_pnl_baseline"] = _daily_pnl_baseline
                        _state["daily_pnl_baseline_date"] = datetime.now().date().isoformat()
                        _state["daily_halt"] = False
                        _save_state(_state)
                        tg.send_text(token, chat,
                                     f"🔄 รีเซ็ตโควต้าขาดทุนวันนี้แล้ว\n"
                                     f"นับขาดทุนใหม่จาก ${_daily_pnl_baseline:.2f} · เพดาน {max_daily_loss}%\n"
                                     "บอทกลับมาเปิดไม้ได้แล้ว ✅")
                    elif cmd == "set":
                        # /set KEY=VALUE — แก้ config โดยตรง ไม่ผ่าน AI
                        raw_arg = _args_raw.strip()
                        if "=" not in raw_arg:
                            tg.send_text(token, chat,
                                         "⚙️ รูปแบบ: /set KEY=VALUE\n"
                                         "ตัวอย่าง: /set BREAKEVEN_AT_R=2.0\n"
                                         "         /set USE_HALFTREND=false\n\n"
                                         "⚠️ พิมพ์ /restart หลังแก้ค่าให้มีผล")
                        else:
                            k, v = raw_arg.split("=", 1)
                            k = k.strip().upper(); v = v.strip()
                            if k in _SECRET_KEYS:
                                tg.send_text(token, chat, f"🔒 ไม่อนุญาตแก้ {k} ผ่าน Telegram (secret key)")
                            elif _cfg_write(k, v):
                                tg.send_text(token, chat,
                                             f"✅ บันทึกแล้ว: `{k}={v}`\n"
                                             "⚠️ พิมพ์ /restart เพื่อให้ค่าใหม่มีผล")
                            else:
                                tg.send_text(token, chat, f"❌ เขียน config ไม่สำเร็จ — ตรวจสอบ log")
                    elif cmd == "ai":
                        # /ai <คำขอภาษาธรรมชาติ> — Gemini แปลง → เปลี่ยน config อัตโนมัติ
                        request_text = _args_raw.strip()
                        if not request_text:
                            tg.send_text(token, chat,
                                         "🤖 รูปแบบ: /ai <คำขอ>\n\n"
                                         "ตัวอย่าง:\n"
                                         "  /ai ปิด HalfTrend\n"
                                         "  /ai เพิ่ม breakeven เป็น 2R\n"
                                         "  /ai เปิด VWAP และปิด notify scan\n"
                                         "  /ai เปลี่ยน max risk เป็น 2%\n\n"
                                         "⚠️ พิมพ์ /restart หลังแก้ให้มีผล")
                        else:
                            tg.send_text(token, chat, f"🤔 กำลังถาม Gemini... ({request_text[:40]})")
                            changes = _ai_cfg_edit(request_text, cfg)
                            if not changes:
                                tg.send_text(token, chat,
                                             "❓ Gemini ไม่แน่ใจว่าต้องแก้ค่าไหน\n"
                                             "ลองพิมพ์ชัดขึ้น หรือใช้ /set KEY=VALUE โดยตรง")
                            else:
                                done, fail = [], []
                                for ch in changes:
                                    k = ch.get("key", "").upper()
                                    v = str(ch.get("value", ""))
                                    r = ch.get("reason", "")
                                    if _cfg_write(k, v):
                                        done.append(f"  ✅ `{k}={v}` — {r}")
                                    else:
                                        fail.append(f"  ❌ {k} (เขียนไม่ได้)")
                                lines = ["🤖 AI แก้ config แล้ว:"] + done + fail
                                lines.append("\n⚠️ พิมพ์ /restart เพื่อให้ค่าใหม่มีผล")
                                tg.send_text(token, chat, "\n".join(lines))
                    elif cmd == "pause":
                        _set_pause(True)
                        tg.send_text(token, chat, "⏸️ หยุดเปิดไม้ใหม่แล้ว — ไม้ที่เปิดอยู่ยังจัดการต่อ (พิมพ์ /resume เพื่อเริ่มใหม่)")
                    elif cmd == "resume":
                        _set_pause(False)
                        tg.send_text(token, chat, "▶️ กลับมาเปิดไม้อัตโนมัติแล้ว")
                    elif cmd == "scan":
                        # /scan — สแกนทันทีโดยไม่รอรอบปกติ + ส่งผลมาที่ Telegram
                        if not _ensure_connected(cfg):
                            tg.send_text(token, chat, "❌ ต่อ MT5 ไม่ได้ — ลองใหม่อีกสักครู่")
                        elif daily_halt:
                            tg.send_text(token, chat,
                                         "🛑 บอทหยุด (daily halt) — พิมพ์ /reset_daily ก่อน\n"
                                         "จะยังดูผล scan ไม่ได้จนกว่าจะ reset")
                        elif _is_paused():
                            tg.send_text(token, chat,
                                         "⏸️ บอท pause อยู่ — พิมพ์ /resume แล้วค่อย /scan")
                        else:
                            tg.send_text(token, chat,
                                         "🔍 กำลังสแกนตลาด...\n"
                                         "ผลจะส่งมาใน ~10 วิ (รอ loop รอบถัดไป)")
                            last_scan = 0.0          # รีเซ็ต timer → scan ทันทีรอบถัดไป
                            last_scan_key = None     # อนุญาตส่ง summary แม้ผลเหมือนรอบก่อน
                            queue.clear()            # ล้างสัญญาณเก่าที่ค้างอยู่
                            _scan_requested = True   # flag: ส่งผล Telegram แม้ notify_scan=false
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

            # 2.4) เบรกขาดทุนสะสม (drawdown จากจุดสูงสุด) → หยุดเปิดไม้ใหม่ (+ปิดไม้ ถ้าตั้งไว้) + แจ้ง
            if max_dd > 0 and eq > 0:
                if eq > peak_eq:
                    peak_eq = eq
                    _state["peak_eq"] = peak_eq; _save_state(_state)
                elif peak_eq > 0 and eq <= peak_eq * (1 - max_dd / 100) and not _is_paused():
                    dd = (peak_eq - eq) / peak_eq * 100
                    # ดีฟอลต์: หยุดเปิดไม้ใหม่อย่างเดียว ไม่ปิดไม้เดิม (ตามกฎ "ขาดทุนไม่ปิด ปล่อยถึง SL เอง")
                    # เปิด DRAWDOWN_CLOSE_POSITIONS=true ถ้าอยากให้ปิดไม้ทั้งหมดด้วย (เกราะแข็งกว่า)
                    _dd_close = cfg.get("DRAWDOWN_CLOSE_POSITIONS", "false").lower() in ("1", "true", "yes", "on")
                    _act = "หยุดเปิดไม้ใหม่ + ปิดไม้ทั้งหมด" if _dd_close else "หยุดเปิดไม้ใหม่ (ไม้เดิมวิ่งต่อถึง SL เอง)"
                    tg.send_text(token, chat, f"🛑 ขาดทุนสะสมถึง {dd:.1f}% (เพดาน {max_dd:.0f}%) — "
                                              f"{_act} · พิมพ์ /resume เพื่อเริ่มใหม่")
                    if execute_on and _dd_close:
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
                        emo = "✅" if d["profit"] >= 0 else "❌"
                        dir_th = "🟢 Buy" if d.get("direction") == "buy" else "🔴 Sell"
                        # เหตุผลปิด: TP / SL / Bot ปิดเอง / Manual / Stop Out
                        # ไม่รู้จริง ("") → ไม่โชว์ป้าย (ดีกว่าเดาผิดแล้วทำคนสับสน)
                        _reason_map = {"tp": "🎯 TP", "sl": "🛑 SL",
                                       "bot": "🤖 Bot", "manual": "✋ Manual",
                                       "so": "💥 Stop Out"}
                        reason_str = _reason_map.get(d.get("close_reason", ""), "")
                        reason_line = f" · {reason_str}" if reason_str else ""
                        pct = (f" = {d['profit'] / bal * 100:+.2f}% ของทุน ${bal:,.0f}"
                               if bal and bal > 0 else "")
                        day = journal.today_pnl()
                        # หาชื่อเทคนิคจาก position_id → trade meta
                        _src = _meta.get(str(d.get("position_id", "")), "")
                        _src_label = _SRC_MAP.get(_src, "")
                        src_line = f"\n⚙️ {_src_label}" if _src_label else ""
                        tg.send_text(token, chat,
                                     f"{emo} ปิดไม้{reason_line} — {d['symbol']} {dir_th} {d['volume']} lot{src_line}\n"
                                     f"💰 P/L ${d['profit']:+.2f}{pct}\n"
                                     f"📊 รวมวันนี้ ${day:+.2f}")
                except Exception as _je:  # noqa: BLE001
                    log.warning("journal รายงานปิดไม้ fail: %s", _je)
                # learn แยก try — ไม่ให้ journal error กลบ outcome attachment
                try:
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

            # 2.7) เบรกขาดทุนต่อวัน — นับจาก _daily_pnl_baseline (reset ได้ด้วย /reset_daily)
            if bal > 0:
                dpnl = journal.today_pnl() - _daily_pnl_baseline
                if dpnl <= -(bal * max_daily_loss / 100.0):
                    if not daily_halt:
                        tg.send_text(token, chat,
                                     f"🛑 หยุดเทรดวันนี้ — ขาดทุน ${dpnl:.2f} ถึงเพดาน {max_daily_loss}%\n"
                                     "ไม้เก่ายังจัดการต่อ · พิมพ์ /reset_daily ถ้าต้องการเปิดโควต้าใหม่")
                        daily_halt = True
                        # บันทึก halt ลง state — ถ้า restart จะดึงกลับมา (ป้องกันหลบเพดานด้วยการ restart)
                        _state["daily_halt"] = True
                        _state["daily_pnl_baseline_date"] = datetime.now().date().isoformat()
                        _save_state(_state)
                # ⚠️ ห้าม auto-clear daily_halt แม้ floating กลับมา
                # เพราะทำให้ขาดทุนได้ 2× ในวันเดียว — ต้องพิมพ์ /reset_daily เพื่อเปิดโควต้าใหม่

            # 3) หาโอกาสใหม่ (ถ้าไม่ halt · ไม่ pause · ไม่ใช่ช่วงข่าวแรง)
            open_n = _count_open() if auto_on else 0
            _now_full = auto_on and open_n >= max_pos

            # ── ตรวจการเปลี่ยนสถานะ "ไม้เต็ม ↔ ไม้ว่าง" ─────────────────────────
            # แจ้งแค่ตอน transition ไม่แจ้งซ้ำทุกรอบ
            if _now_full and not _pos_full:
                tg.send_text(token, chat,
                             f"📊 ไม้เต็มแล้ว ({open_n}/{max_pos}) — หยุดสแกนชั่วคราว รอไม้ปิด")
                log.info("ไม้เต็ม %d/%d → หยุดสแกน", open_n, max_pos)
            elif _pos_full and not _now_full:
                tg.send_text(token, chat,
                             f"🔓 ไม้ว่างแล้ว ({open_n}/{max_pos}) — กลับมาสแกน")
                log.info("ไม้ว่าง %d/%d → กลับมาสแกน", open_n, max_pos)
            _pos_full = _now_full

            # มีไม้เพิ่งปิด (TP/SL) → สแกนหาตัวใหม่โดยเร็ว
            # กำหนด minimum 60s กัน MT5 connection กระพริบแล้วทำ force-rescan ซ้ำทุกรอบ
            if auto_on and open_n < prev_open and now - last_scan > 60:
                last_scan = 0.0
                last_scan_key = None  # reset เพื่อให้ Telegram ส่ง summary scan ใหม่หลังไม้ปิด
                queue.clear()   # ล้าง queue เก่า — สัญญาณอาจ stale หลังจากไม้ปิด
                log.info("มีไม้ปิด เหลือ %d ไม้ → สแกนใหม่ทันที (queue ล้างแล้ว)", open_n)
            prev_open = open_n
            blocked = daily_halt or _is_paused()
            slot_free = (open_n < max_pos) if auto_on else (pending is None)
            if slot_free and not blocked:
                blackout, ev = news_guard.is_blackout(finnhub, blackout_min)
                if blackout:
                    if not _in_blackout:            # แจ้ง Telegram แค่ครั้งแรกที่เข้าช่วงข่าว
                        tg.send_text(token, chat, f"⏸️ งดเปิดไม้ชั่วคราว — ใกล้ข่าวแรง: {ev}")
                        _in_blackout = True
                    if now - last_scan > scan_gap:
                        log.info("งดเปิดไม้ — ใกล้ข่าวแรง: %s", ev)
                        last_scan = now
                else:
                    if _in_blackout:                # ออกจาก blackout — แจ้งกลับมาสแกนปกติ
                        tg.send_text(token, chat, "✅ พ้นช่วงข่าวแรงแล้ว — กลับมาสแกนปกติ")
                        _in_blackout = False
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
                        if use_ema_m5 and auto_on:         # EMA Ribbon M5 — FX scalp (prop firm strategy)
                            queue += _scan_ema_m5(cfg, syms)
                        if use_fx_orb and auto_on:         # FX ORB London
                            queue += _scan_fx_orb(cfg, syms)
                        if use_range_mr and auto_on:       # Range-edge mean reversion (ตลาด sideways)
                            queue += _scan_range_mr(cfg, syms)
                        if use_rvol_brk and auto_on:       # RVOL spurt + breakout (in-play momentum)
                            queue += _scan_rvol_brk(cfg, syms)
                        if use_hybrid and auto_on:         # Hybrid-Pro H1+M15
                            queue += _scan_hybrid(cfg, syms)
                        if use_pa:                         # Price Action & Market Structure H1
                            queue += _scan_pa(cfg, syms)
                        if use_vwap and auto_on:           # VWAP Bounce M5 (Jane Street / Citadel)
                            queue += _scan_vwap(cfg, syms)
                        if use_bb_squeeze and auto_on:     # BB Squeeze Breakout M5
                            queue += _scan_bb_squeeze(cfg, syms)
                        if use_rsi_div and auto_on:        # RSI Divergence M5
                            queue += _scan_rsi_div(cfg, syms)
                        if use_orb_pro and auto_on:        # ORB London/NY session (Toby Crabel)
                            queue += _scan_orb_pro(cfg, syms)
                        _annotate_confluence(queue)        # นับกลยุทธ์ที่เห็นพ้อง → boost lot ใน build_ticket
                        last_scan = now
                        log.info("สแกนได้ %d ตัวมีทิศ", len(queue))
                        # ผู้ใช้ /scan แต่ไม่มีสัญญาณเลย → แจ้งทันที
                        if _scan_requested and not queue:
                            tg.send_text(token, chat,
                                         "🔍 สแกนเสร็จแล้ว — ไม่พบสัญญาณ ณ ขณะนี้\n"
                                         "(ตลาดนิ่ง หรือยังอยู่นอกเวลาเปิด)")
                            _scan_requested = False
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
                            # ข้ามเงียบถ้าตลาดปิด — ไม่เพิ่มใน scan_res / ไม่เรียก build_ticket
                            # (หุ้น US pre-market: spread 5–10× กว้างกว่าปกติ → ไม่มีประโยชน์ประมวล)
                            if not market_hours.is_open(sym):
                                continue
                            # crypto blackout เพิ่มเติม (ช่วงบาง + options expiry)
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
                                # FX session mode: ปลดลิมิตกลุ่ม FX เป็น FX_MAX_POSITIONS
                                # (ช่วงตลาด US ไม่ต้องเช็ค — build_ticket บล็อกเปิดไม้ FX อยู่แล้ว)
                                if fx_session_mode and grp == "FX" and fx_max_pos > 0:
                                    lim = fx_max_pos
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
                        if scan_res:
                            # scan summary → log เสมอ (เพื่อ debug ใน part2.log)
                            log.info("scan: %s", "; ".join(
                                f"{'✅' if st=='open' else '⛔'} {s} {dr} {detail or ''}"
                                for s, dr, st, detail in scan_res))
                            # แจ้ง Telegram เมื่อ: NOTIFY_SCAN=true หรือ ผู้ใช้พิมพ์ /scan
                            # throttle ปกติยังใช้อยู่สำหรับ notify_scan (กัน spam)
                            # แต่ /scan ข้าม throttle ได้เสมอ — ผู้ใช้สั่งเองจึงต้องการเห็นผล
                            if notify_scan or _scan_requested:
                                kset = frozenset((s, st) for s, _, st, _ in scan_res)
                                if _scan_requested or (kset != last_scan_key and now - last_notify_ts >= scan_gap):
                                    tg.send_text(token, chat, _scan_summary(scan_res))
                                    last_scan_key = kset
                                    last_notify_ts = now
                            _scan_requested = False   # reset ไม่ว่าจะส่งหรือไม่
                        elif _scan_requested:
                            # scanner วิ่งแล้ว มีสัญญาณ แต่ทุกตัวถูกกรองออก (spread/RSI/ไม้เต็ม)
                            tg.send_text(token, chat,
                                         "🔍 สแกนเสร็จ — พบสัญญาณแต่ผ่านด่านไม่ได้\n"
                                         "(spread กว้าง / RSI ออกโซน / ไม้เต็ม / ตลาดปิด)")
                            _scan_requested = False
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
                            if not market_hours.is_open(bias["symbol"]):  # ตลาดปิด → ไม่เสนอ
                                continue
                            # ส่ง scalp= เพื่อให้ build_ticket ใช้ SL/RR ของกลยุทธ์ (เหมือน auto mode)
                            t = tk.build_ticket(bias["symbol"], bias, acc_now, cfg, m, part1_hint=hint,
                                                scalp=bias.get("scalp"))
                            if not t or t.get("skipped") or t["verdict"].get("decision") not in accept:
                                continue
                            tid = f"t{counter}"; counter += 1
                            text = tk.format_ticket(t) + f"\n\n⏳ ตอบใน {ttl // 60} นาที ไม่งั้นใบสั่งจะหายไป"
                            mid = tg.send_ticket(token, chat, text, tid)
                            if mid:
                                pending = {"tid": tid, "msg_id": mid, "ticket": t, "sent_at": now}
                                recent[key] = now
                                log.info("เสนอใบสั่ง %s %s", t["exsym"], t["direction"])
            # 4) Heartbeat — แจ้งสถานะทุก N นาที (กัน user คิดว่าบอทหยุดทำงาน)
            if heartbeat_sec > 0 and now - last_heartbeat >= heartbeat_sec:
                _hb_n = _count_open()
                if daily_halt:
                    _hb_why = "🛑 หยุดวันนี้ (ขาดทุนถึงเพดาน)"
                elif _is_paused():
                    _hb_why = "⏸️ หยุดชั่วคราว — กด /resume เพื่อเริ่ม"
                elif auto_on and _hb_n >= max_pos:
                    _hb_why = f"📊 ไม้เต็ม {_hb_n}/{max_pos} ไม้ — รอไม้ปิด"
                else:
                    _hb_why = f"🔍 สแกนต่อเนื่อง · ไม้เปิด {_hb_n}/{max_pos}"
                tg.send_text(token, chat, f"💓 Part 2 ทำงาน · {_hb_why}")
                last_heartbeat = now
            _exc_streak = 0   # reset เมื่อรอบ loop สำเร็จ (ไม่มี exception)
            time.sleep(2)
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            _exc_streak += 1
            log.exception("loop error: %s", e)
            if _exc_streak in (3, 10, 30):      # แจ้ง Telegram เมื่อ error วนซ้ำ (3×, 10×, 30×)
                try:
                    tg.send_text(token, chat,
                                 f"⚠️ Part 2: loop error ซ้ำ {_exc_streak}× — {str(e)[:100]}\n"
                                 "บอทยังรัน (retry อัตโนมัติ) ถ้าไม่กลับมาใน 5 นาที ให้ /restart")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(5)
    m.shutdown()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise                                   # ปล่อย sys.exit(2) ผ่าน (bat ใช้ errorlevel)
    except BaseException:                        # noqa: BLE001 — crash ตอน startup/หลุด loop
        log.exception("fatal error — interactive.py หลุดออก (bat จะ restart)")
        raise
