"""
bot.py — Telegram webhook สำหรับจัดการ Watchlist (รันเป็น Cloud Run Service)

คำสั่ง (เปิด ↔ ปิด):
  /buy SYM [ราคา]      ↔ /sell SYM      — Spot
  /callbuy SYM [ราคา]  ↔ /callsell SYM  — Call (เก็งขึ้น)
  /putbuy SYM [ราคา]   ↔ /putsell SYM   — Put (เก็งลง)
  /list   — ดูทุกตัวที่ถือ + ราคา + %P/L
  /help   — วิธีใช้

ความปลอดภัย:
- ตอบเฉพาะข้อความจาก TELEGRAM_CHAT_ID ที่ตั้งไว้ (คนอื่นสั่งไม่ได้)
- ตรวจ secret token header ของ Telegram (กัน endpoint ถูกยิงมั่ว)

รันด้วย: gunicorn --bind :$PORT bot:app
"""
from __future__ import annotations
import logging
import os
import sys
from datetime import datetime, timezone

from flask import Flask, request

from core.symbols import resolve_symbol, Resolved
from core.signals import zone_label, compute_signal
from data.quote import fetch_history
from notify.telegram import send_telegram
from watchlist import store
from watchlist import journal as wl_journal
from watchlist.tracker import (
    make_position, quick_status, full_status, format_risk_levels,
    time_to_target_hint, format_option_thesis,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
CRYPTO_EXCHANGE = os.getenv("CRYPTO_EXCHANGE", "binance").strip().lower()
SCAN_JOB = os.getenv("SCAN_JOB_NAME", "cdc-scanner").strip()
SCAN_REGION = os.getenv("SCAN_JOB_REGION", "asia-southeast1").strip()
GCP_PROJECT = os.getenv("GCP_PROJECT", "").strip()

# สภาพคล่อง option (กันแนะนำตัวขายต่อยาก) — ให้ตรงกับ config.py
_OPT_LIQ_ON = os.getenv("ENABLE_OPTION_LIQUIDITY", "true").strip().lower() in ("1", "true", "yes", "y", "on")
_MIN_OI = int(os.getenv("MIN_OPTION_OI", "500") or "500")
_MAX_SPREAD = float(os.getenv("MAX_OPTION_SPREAD", "40") or "40")


def _trigger_scan_job(group: str | None = None) -> bool:
    """
    สั่งให้ Cloud Run Job cdc-scanner รัน (ผลส่งเข้า Telegram เองตอนจบ)
    group=None → สแกนทุกกลุ่ม; ถ้าระบุ → override env SCAN_GROUPS เฉพาะ execution นี้
    """
    import google.auth
    from google.auth.transport.requests import Request as _GAReq
    import requests as _rq

    creds, proj = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(_GAReq())
    project = GCP_PROJECT or proj
    url = (f"https://run.googleapis.com/v2/projects/{project}"
           f"/locations/{SCAN_REGION}/jobs/{SCAN_JOB}:run")
    body: dict = {}
    if group:
        # env override merge เฉพาะ execution นี้ (secret ของ job คงอยู่)
        body = {"overrides": {"containerOverrides": [
            {"env": [{"name": "SCAN_GROUPS", "value": group}]}
        ]}}
    r = _rq.post(
        url,
        headers={"Authorization": f"Bearer {creds.token}",
                 "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    if r.status_code not in (200, 201):
        log.error("trigger scan failed: %s %s", r.status_code, r.text[:200])
        return False
    return True


# alias ที่ผู้ใช้พิมพ์ → คีย์หมวด (ตรงกับ GROUP_RUNNERS ใน main.py)
_SCAN_ALIASES = {
    "crypto": "crypto",
    "usstocks": "usstocks", "us": "usstocks", "usstock": "usstocks", "stocks": "usstocks",
    "thaistocks": "thaistocks", "thai": "thaistocks", "thaistock": "thaistocks", "set": "thaistocks",
    "commodity": "commodity", "commodities": "commodity", "metal": "commodity", "metals": "commodity",
}
_SCAN_LABEL = {
    "crypto": "Crypto", "usstocks": "US Stocks",
    "thaistocks": "Thai Stocks", "commodity": "Commodities",
}
# กลุ่ม (ที่ผู้ใช้พิมพ์) → market สำหรับ "บังคับตลาด" ตอนสแกนตัวเดียวนอก universe
_GROUP_TO_MARKET = {
    "crypto": "crypto", "usstocks": "us",
    "thaistocks": "thai", "commodity": "commodity",
}
# สถานะโซนปัจจุบัน (ภาษาธรรมชาติ) — โชว์เป็นบรรทัดหลักของผลสแกนตัวเดียว
_ZONE_STATUS = {
    "green":  "🟢 กราฟเป็นขาขึ้น ยืนเหนือ EMA12",
    "yellow": "🟡 กราฟขาขึ้นแต่กำลังย่อ (หลุด EMA12)",
    "orange": "🟠 กราฟปลายขาขึ้น ย่อแรง ใกล้กลับตัว",
    "red":    "🔴 กราฟเป็นขาลง อยู่ใต้ EMA12",
    "lblue":  "🩵 กราฟขาลงแต่เริ่มเด้ง (เหนือ EMA12)",
    "blue":   "🔵 กราฟปลายขาลง เด้งแรง ใกล้กลับตัว",
}
# อ่านโซน → จับตาอะไรต่อ (ตอนยังไม่มีสัญญาณใหม่)
_ZONE_READ = {
    "green":  "ยืนเหนือ EMA12 ได้ = ขาขึ้นยังแข็งแรง · หลุด EMA12 เมื่อไหร่ → 🟡 เริ่มย่อ",
    "yellow": "หลุด EMA12 แล้ว · รอกลับขึ้นเหนือ EMA12 (→🟢 ไปต่อ) หรือหลุด EMA26 (→🟠 ย่อแรง)",
    "orange": "ใกล้กลับตัว! ถ้า EMA12 ตัดลงใต้ EMA26 จะกลายเป็น 🔴 (สัญญาณ Sell)",
    "red":    "อยู่ใต้ EMA12 = ขาลงยังแข็งแรง · เด้งเหนือ EMA12 เมื่อไหร่ → 🩵 เริ่มเด้ง",
    "lblue":  "เด้งเหนือ EMA12 แล้ว · รอเด้งเหนือ EMA26 (→🔵) หรือหลุดกลับ (→🔴 ลงต่อ)",
    "blue":   "ใกล้กลับตัว! ถ้า EMA12 ตัดขึ้นเหนือ EMA26 จะกลายเป็น 🟢 (สัญญาณ Buy)",
}


def _resolve_forced(group_key: str, raw: str) -> Resolved:
    """บังคับตลาดตามที่ผู้ใช้ระบุ (เช่น /scan us SOFI) — รองรับหุ้นนอก universe"""
    s = (raw or "").strip().upper()
    market = _GROUP_TO_MARKET[group_key]
    if market == "us":
        return Resolved("us", s, s)
    if market == "thai":
        ticker = s if s.endswith(".BK") else f"{s}.BK"
        return Resolved("thai", ticker, s.replace(".BK", ""))
    if market == "crypto":
        ticker = s if "/" in s else f"{s}/USDT"
        return Resolved("crypto", ticker, ticker)
    # commodity — ใช้ alias โลหะถ้ามี (XAUUSD→GC=F) ไม่งั้นถือเป็น yahoo ticker ตรง ๆ
    from core.symbols import _COMMODITY_ALIAS
    if s in _COMMODITY_ALIAS:
        tk, disp = _COMMODITY_ALIAS[s]
        return Resolved("commodity", tk, disp)
    return Resolved("commodity", s, s)


def _resolve_cmd_symbol(args: list[str]):
    """resolve args[0] → Resolved · ถ้ามี keyword ตลาด (us/thai/crypto/commodity) ใน args
    บังคับตลาดนั้น (กันหุ้น US โดนเดาเป็น crypto) · คืน (Resolved, args ที่ตัด keyword ออกแล้ว)"""
    sym = args[0]
    forced = None
    rest = [sym]
    for a in args[1:]:
        g = _SCAN_ALIASES.get(a.lower())
        if g and forced is None:
            forced = g                       # keyword ตลาดตัวแรกที่เจอ (หลัง symbol)
        else:
            rest.append(a)
    resolved = _resolve_forced(forced, sym) if forced else resolve_symbol(sym)
    return resolved, rest


def _confidence(s) -> str:
    """ระดับความเชื่อมั่นจาก score + weekly (ใช้ทั้งกรณีมีสัญญาณและอ่านจากโซน)"""
    if s.high_quality and s.mtf_aligned is True:
        return "สอดคล้องทุกชั้น น่าสนใจ"
    if s.high_quality:
        return "พื้นฐานดี แต่ weekly ยังไม่ยืนยัน — ไม้เล็ก/รอ confirm"
    if (s.score or 0) >= 2:
        return "ปานกลาง — รอ confirm หรือไม้เล็ก"
    return "อ่อน — แนะนำรอจังหวะ"


def _liq_exp(liq) -> str:
    """วันหมดอายุของ expiry ที่ใช้เช็กสภาพคล่อง (DD/MM) — ตรงกับ DTE ที่แนะนำ"""
    e = liq.get("expiry") if liq else None
    if not e:
        return ""
    try:
        _, m, d = str(e).split("-")
        return f"{d}/{m}"
    except Exception:  # noqa: BLE001
        return ""


def _single_reco(s) -> str:
    """แนะนำทิศ Option — มีสัญญาณ→ใช้ของสแกนกลุ่ม; ไม่มี→อนุมานจากโซน (สภาพคล่องแยกไป _option_guidance)"""
    if s.signal in ("buy", "sell"):
        from main import _recommendation
        return _recommendation(s)
    conf = _confidence(s)
    if s.zone == "green":
        return f"👉 ทิศหลักขาขึ้น → ถือ/เปิด Call ได้ ({conf})"
    if s.zone == "red":
        return f"👉 ทิศหลักขาลง → ถือ/เปิด Put ได้ ({conf})"
    if s.zone in ("yellow", "orange"):
        return f"👉 ทิศหลักยังขาขึ้น (Call) แต่กำลังย่อชั่วคราว — รอราคากลับเหนือ EMA12 ค่อยเข้า ({conf})"
    if s.zone in ("lblue", "blue"):
        return f"👉 ทิศหลักยังขาลง (Put) แต่กำลังเด้งชั่วคราว — รอราคาหลุดกลับใต้ EMA12 ค่อยเข้า ({conf})"
    return ""


def _format_single(s, resolved: Resolved, extra: str = "") -> str:
    """ฟอร์แมตผลสแกนตัวเดียว — ครบเท่าสแกนกลุ่ม: ดาว + breakdown + แนะนำ Option"""
    # reuse ตัวจัดข้อความเดียวกับสแกนกลุ่ม (กันรูปแบบเพี้ยน)
    from main import (_projection, _time_hint, _transition_line, _option_guidance,
                      _stage_line, _entry_line, _trend_position, _is_sideway, _trend_quality_line)

    star = (s.score or 0) + (1 if s.mtf_aligned is True else 0)
    stars_txt = f" {'⭐' * star}" if star else ""
    hq = " HQ" if s.high_quality else ""
    has_signal = s.signal in ("buy", "sell")

    lines = [f"🔎 {resolved.display} @ {_fmt_price(s.close)}  ({resolved.market})"]

    if has_signal:
        head = "🟢 วันนี้: CDC BUY" if s.signal == "buy" else "🔴 วันนี้: CDC SELL"
        lines.append(f"{head}{stars_txt}{hq}")
        tl = _transition_line(s)
        if tl:
            lines.append(tl)
    else:
        # บรรทัดสถานะโซน (ภาษาธรรมชาติ) + ดาวคุณภาพของทิศหลัก
        lines.append(f"{_ZONE_STATUS.get(s.zone, zone_label(s.zone))}{stars_txt}{hq}")
        lines.append("(ยังไม่มีสัญญาณใหม่วันนี้ — อ่านจากโซนปัจจุบัน)")

    sl = _stage_line(s)  # ภาพใหญ่เทรนด์ (Weinstein Stage)
    if sl:
        lines.append(sl)
    tq = _trend_quality_line(s)  # คุณภาพเทรนด์ (R² เนียน/ขรุขระ)
    if tq:
        lines.append(tq)

    # breakdown ✅/❌ มีทั้งสองกรณี (RSI/Volume/ADX/EMA200 พร้อมความหมาย)
    for b in s.breakdown:
        lines.append(("✅ " if b["ok"] else "❌ ") + b["text"])

    reco = _single_reco(s)
    if reco:
        lines.append(reco)

    el = _entry_line(s)  # โซนราคาเข้าที่เหมาะ (แนวรับ/ต้าน EMA)
    if el:
        lines.append(el)
    tp = _trend_position(s)  # ต้น/กลาง/ปลายเทรนด์ (กันดอย)
    if tp:
        lines.append(tp)
    if _is_sideway(s):  # /scan รายตัวไม่ตัด แต่ติดป้ายเตือน
        lines.append("↔️ คาดว่า Sideway — ยังไม่มีเทรนด์ชัด รอ breakout ค่อยเข้า")

    if has_signal:  # มีจุดเข้าจริง → คาดเป้า + เวลาถึงเป้า
        lines.extend(_projection(s))
        th = _time_hint(s)
        if th:
            lines.append(th)
    else:  # ยังไม่มีจุดเข้า → คาดเป้า/เวลา ตามทิศโซนปัจจุบัน + จับตาอะไรต่อ
        bias = ("buy" if s.zone in ("green", "yellow", "orange")
                else "sell" if s.zone in ("red", "lblue", "blue") else None)
        if bias:
            lines.extend(_projection(s, direction=bias))
            th = _time_hint(s)
            if th:
                lines.append(th)
        read = _ZONE_READ.get(s.zone)
        if read:
            lines.append(f"💡 {read}")

    g = _option_guidance(s)  # บรรทัด option (สัญญาแนะนำ/ไม่คล่อง/ไม่มีข้อมูล) เฉพาะหุ้น US
    if g:
        lines.append(g)

    if extra:  # บล็อกข้อมูลเชิง option (IV/HV/งบ) วางก่อนบรรทัดวันที่
        lines.append("")
        lines.append(extra)

    cf = _conflict_note(s)  # H) AI อธิบายถ้าสัญญาณสั้นขัดภาพใหญ่/RS
    if cf:
        lines.append("")
        lines.append(cf)

    lines.append(f"\n📅 อ้างอิงแท่งปิด: {s.bar_date.strftime('%Y-%m-%d')}")
    return "\n".join(lines)


def _conflict_note(s) -> "str | None":
    """H) ตรวจสัญญาณขัดแย้ง (สั้น vs ภาพใหญ่/RS) → ให้ Gemini อธิบายสั้น ๆ · None ถ้าไม่ขัด/ไม่มี AI"""
    try:
        from data import ai
    except Exception:  # noqa: BLE001
        return None
    if not ai.enabled():
        return None
    st = s.stage or {}
    stage_n = st.get("n")
    bullish = s.signal == "buy" or s.zone in ("green", "yellow", "orange")
    bearish = s.signal == "sell" or s.zone in ("red", "lblue", "blue")
    conflicts: list[str] = []
    if bullish and stage_n == 4:
        conflicts.append("สัญญาณ/โซนระยะสั้นเป็นบวก แต่ภาพใหญ่เป็น Stage 4 (ขาลง)")
    if bullish and s.rs_rank is not None and s.rs_rank < 40:
        conflicts.append(f"ฝั่งซื้อ แต่ RS อ่อน ({s.rs_rank:.0f}) อ่อนกว่าตลาดส่วนใหญ่")
    if bearish and stage_n == 2:
        conflicts.append("สัญญาณ/โซนระยะสั้นเป็นลบ แต่ภาพใหญ่ยังเป็น Stage 2 (ขาขึ้น)")
    if not conflicts:
        return None
    prompt = (
        f"หุ้น {s.display_name} มีสัญญาณขัดแย้งกัน: {'; '.join(conflicts)}. "
        f"ข้อมูลจริง: zone={s.zone}, stage={st.get('label')}, RS={s.rs_rank}, RSI={s.rsi}, signal_today={s.signal}. "
        "อธิบายสั้น ๆ เป็นไทย 2-3 บรรทัด ว่าความขัดแย้งนี้บอกอะไร และควรพิจารณาอย่างไรเชิงเฝ้าระวัง "
        "(ไม่ใช่สั่งซื้อขาย) อิงข้อมูลที่ให้เท่านั้น ใช้ข้อความธรรมดา (ห้าม markdown)."
    )
    out = ai.gemini(prompt, temperature=0.3)
    if not out or not str(out).strip():
        return None
    return "⚖️ มุมมองสัญญาณขัดแย้ง:\n" + str(out).strip()


def _scan_symbol(resolved: Resolved) -> "str | None":
    """ดึงข้อมูล + คำนวณ CDC ของ symbol เดียว → ข้อความ (None = ไม่พบข้อมูล)"""
    df = fetch_history(resolved.market, resolved.data_ticker, crypto_exchange=CRYPTO_EXCHANGE)
    if df is None or df.empty:
        return None
    # score_when_none=True → คิดดาว/breakdown จากโซนปัจจุบันด้วย (ใช้เฉพาะสแกนตัวเดียว)
    sig = compute_signal(
        df, resolved.data_ticker, display_name=resolved.display,
        enable_ema200_filter=True, min_bars_required=60, enable_mtf=True,
        score_when_none=True,
    )
    if sig is None:  # ข้อมูล < 200 แท่ง → ปิด EMA200 (ดาวจะไม่มีคะแนนเทรนด์ใหญ่)
        sig = compute_signal(
            df, resolved.data_ticker, display_name=resolved.display,
            enable_ema200_filter=False, min_bars_required=30, enable_mtf=True,
            score_when_none=True,
        )
    if sig is None:
        return None
    # มุม option (IV/HV/งบ) + สภาพคล่อง — เฉพาะตลาดที่เทรด option (ไม่ใช่ crypto)
    extra = ""
    if resolved.market != "crypto":
        try:
            from data.market import options_context, option_liquidity
            from watchlist.tracker import recommended_min_dte
            spot = float(df["close"].iloc[-1])
            liq = None
            if _OPT_LIQ_ON and resolved.market == "us":
                dte = recommended_min_dte(sig.atr, sig.adx)  # เช็ก expiry ที่ ⏱️ แนะนำ
                liq = option_liquidity(resolved.data_ticker, spot, target_dte=dte, min_oi=_MIN_OI, max_spread_pct=_MAX_SPREAD)
                sig.option_liq = liq  # ให้คำแนะนำ Call/Put สะท้อนสภาพคล่อง
                from data.market import liq_cache_put_many
                liq_cache_put_many({resolved.data_ticker: liq})  # เก็บค่าดีไว้ใช้ตอน yahoo ว่าง
            ctx = options_context(resolved.market, resolved.data_ticker, df=df, spot=spot, liq=liq)
            if ctx:
                extra = "🎬 มุม Option:\n" + ctx
        except Exception as e:  # noqa: BLE001
            log.warning("options_context (scan %s) failed: %s", resolved.data_ticker, e)

    # มิติพื้นฐาน + นักวิเคราะห์ + ข่าว + insider (Finnhub/FMP) — เฉพาะหุ้น US
    if resolved.market == "us":
        try:
            from data.fundamentals import fundamental_block
            fb = fundamental_block(resolved.data_ticker)
            if fb:
                extra = (extra + "\n\n" + fb) if extra else fb
        except Exception as e:  # noqa: BLE001
            log.warning("fundamental_block (scan %s) failed: %s", resolved.data_ticker, e)
    return _format_single(sig, resolved, extra)

# คำสั่งเปิด/ปิด → side
_OPEN = {"buy": "spot", "callbuy": "call", "putbuy": "put"}
_CLOSE = {"sell": "spot", "callsell": "call", "putsell": "put"}
_SIDE_TH = {"spot": "Spot", "call": "Call", "put": "Put"}

app = Flask(__name__)


def _reply(text: str) -> None:
    if BOT_TOKEN and CHAT_ID:
        send_telegram(text, token=BOT_TOKEN, chat_id=CHAT_ID)


def _fmt_price(p) -> str:
    if p is None:
        return "—"
    return f"{p:,.4f}".rstrip("0").rstrip(".") if p < 100 else f"{p:,.2f}"


def _fmt_pnl(pnl) -> str:
    if pnl is None:
        return ""
    arrow = "📈" if pnl >= 0 else "📉"
    return f" {arrow} {pnl:+.2f}%"


def _handle_open(cmd: str, args: list[str]) -> str:
    side = _OPEN[cmd]
    is_option = side in ("call", "put")
    if not args:
        if is_option:
            return f"ใช้: /{cmd} SYMBOL [ตลาด] [strike] [ราคาหุ้น]\nเช่น /{cmd} CLX 80 89 · ระบุตลาด /{cmd} CLX us 80 89"
        return f"ใช้: /{cmd} SYMBOL [ตลาด] [ราคาเข้า]\nเช่น /{cmd} SOL 150 · ระบุตลาด /{cmd} SOFI us"
    try:
        resolved, args = _resolve_cmd_symbol(args)
    except (ValueError, KeyError):
        return "❌ ไม่เข้าใจ symbol"

    if is_option:
        return _open_option(cmd, side, resolved, args)
    return _open_spot(cmd, side, resolved, args)


def _open_spot(cmd: str, side: str, resolved, args: list[str]) -> str:
    entry_price = None
    if len(args) >= 2:
        try:
            entry_price = float(args[1].replace(",", ""))
        except ValueError:
            return f"❌ ราคาไม่ถูกต้อง: {args[1]}"

    pos = make_position(resolved, side, entry_price, crypto_exchange=CRYPTO_EXCHANGE)
    store.add_position(pos)

    zone_txt = f" | โซนตอนเข้า: {pos['entry_zone']}" if pos.get("entry_zone") else ""
    price_src = "(คุณระบุ)" if entry_price is not None else "(ราคาตลาดล่าสุด)"

    return (
        f"✅ บันทึก {_SIDE_TH[side]}: {pos['display']} ({resolved.market})\n"
        f"ราคาเข้า: {_fmt_price(pos['entry_price'])} {price_src}{zone_txt}"
        f"{format_risk_levels(pos)}"
    )


def _parse_expiry(s: str) -> "str | None":
    """แปลงวันหมดอายุ '17/07' · '17/07/2026' · '2026-07-17' → 'YYYY-MM-DD' (ไม่ใช่วันที่ → None)"""
    s = (s or "").strip()
    from datetime import date, datetime
    try:
        if "-" in s and len(s) >= 8:  # YYYY-MM-DD
            y, m, d = s.split("-")[:3]
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        if "/" in s:  # DD/MM[/YYYY]
            parts = s.split("/")
            d, m = int(parts[0]), int(parts[1])
            y = int(parts[2]) if len(parts) >= 3 else None
            if y and y < 100:
                y += 2000
            if not y:  # ไม่ระบุปี → เดา (ถ้าวันผ่านแล้วใช้ปีหน้า)
                today = datetime.now().date()
                y = today.year
                try:
                    if date(y, m, d) < today:
                        y += 1
                except ValueError:
                    pass
            return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:  # noqa: BLE001
        return None
    return None


def _open_option(cmd: str, side: str, resolved, args: list[str]) -> str:
    """Call/Put — รับ strike [+ วันหมดอายุ + premium ที่จ่าย] → ติดตามราคา option จริง (Massive)"""
    # แยก args: strike (เลขแรก) · expiry (วันที่) · premium/underlying (เลขถัดไป)
    expiry = None
    nums: list[float] = []
    for a in args[1:]:
        e = _parse_expiry(a)
        if e and expiry is None:
            expiry = e
            continue
        try:
            nums.append(float(a.replace(",", "")))
        except ValueError:
            pass
    strike = nums[0] if nums else None
    if expiry:  # โหมดติดตาม premium: เลขที่ 2 = premium ที่จ่าย · ราคาหุ้นดึงอัตโนมัติ
        entry_premium = nums[1] if len(nums) >= 2 else None
        underlying = None
    else:       # โหมดเดิม: เลขที่ 2 = ราคาหุ้นอ้างอิง
        underlying = nums[1] if len(nums) >= 2 else None
        entry_premium = None

    pos = make_position(resolved, side, underlying, strike=strike, crypto_exchange=CRYPTO_EXCHANGE)

    # ติดตาม premium จริงด้วย Massive (ถ้าระบุวันหมดอายุ + หุ้น US + มี API key)
    track_txt = ""
    if expiry and resolved.market == "us" and strike:
        try:
            from data import massive
            if massive.enabled():
                c = massive.find_contract(resolved.data_ticker, side, strike, expiry)
                if c:
                    pos["opt_ticker"] = c["ticker"]
                    pos["opt_expiry"] = c["expiry"]
                    ep = entry_premium if entry_premium is not None else massive.premium(c["ticker"])
                    pos["entry_premium"] = ep
                    src = "(คุณระบุ)" if entry_premium is not None else "(EOD ล่าสุด)"
                    ep_txt = f"{ep:g}" if ep is not None else "—"
                    track_txt = (f"\n🎟️ ติดตาม premium จริง: {c['ticker'].split(':')[1]} (หมดอายุ {c['expiry']})"
                                 f"\n   เข้าที่ ${ep_txt} {src}")
                else:
                    track_txt = f"\n⚠️ ไม่พบ contract {side} strike {strike:g} หมดอายุ ~{expiry} — เช็ก strike/วันให้ตรงกับที่มีจริง"
        except Exception as e:  # noqa: BLE001
            log.warning("massive track (open %s) failed: %s", resolved.data_ticker, e)

    store.add_position(pos)

    u_src = "(คุณระบุ)" if underlying is not None else "(ราคาตลาดล่าสุด)"
    strike_txt = f"Strike: {_fmt_price(pos['strike'])} | " if pos.get("strike") else ""
    zone = pos.get("entry_zone") or "—"

    thesis = format_option_thesis(pos)
    thesis_txt = f"\n\n{thesis}" if thesis else ""

    th = time_to_target_hint(pos.get("atr"), pos.get("adx"), full=True)
    time_txt = f"\n\n{th}" if th else ""

    # มุม option: สัญญาแนะนำ/ไม่คล่อง/ไม่มีข้อมูล (บรรทัดเดียว) + IV/HV/งบ
    ctx_txt = ""
    opt_txt = ""
    try:
        from data.market import options_context, option_liquidity
        from watchlist.tracker import recommended_min_dte
        liq = None
        if _OPT_LIQ_ON and resolved.market == "us":
            dte = recommended_min_dte(pos.get("atr"), pos.get("adx"))  # expiry ที่แนะนำ
            liq = option_liquidity(resolved.data_ticker, pos.get("entry_price"), target_dte=dte, min_oi=_MIN_OI, max_spread_pct=_MAX_SPREAD)
            from main import _option_guidance_from_liq
            g = _option_guidance_from_liq(liq)
            if g:
                opt_txt = f"\n{g}"
        ctx = options_context(resolved.market, resolved.data_ticker, spot=pos.get("entry_price"), liq=liq)
        if ctx:
            ctx_txt = f"\n\n🎬 มุม Option:\n{ctx}"
    except Exception as e:  # noqa: BLE001
        log.warning("options_context (open %s) failed: %s", resolved.data_ticker, e)

    note = ("※ /list ติดตามราคา premium จริง (EOD จาก Massive) แล้ว"
            if pos.get("opt_ticker")
            else "※ /list ติดตาม 'ทิศหุ้นอ้างอิง' — อยากเห็น premium จริง ใส่วันหมดอายุด้วย เช่น "
                 f"/{cmd} {pos['display']} {pos.get('strike') or 'STRIKE'} 17/07")
    return (
        f"✅ บันทึก {_SIDE_TH[side]}: {pos['display']} ({resolved.market})\n"
        f"{strike_txt}ราคาหุ้นอ้างอิงตอนเข้า: {_fmt_price(pos['entry_price'])} {u_src} | โซน CDC: {zone}"
        f"{track_txt}{opt_txt}{thesis_txt}{time_txt}{ctx_txt}\n\n"
        f"{note}"
    )


def _handle_close(cmd: str, args: list[str]) -> str:
    side = _CLOSE[cmd]
    if not args:
        return f"ใช้: /{cmd} SYMBOL [ตลาด]"
    try:
        resolved, args = _resolve_cmd_symbol(args)
    except (ValueError, KeyError):
        return "❌ ไม่เข้าใจ symbol"

    removed = store.remove_position(resolved.data_ticker, side)
    if removed is None:
        return f"⚠️ ไม่พบสถานะ {_SIDE_TH[side]} ของ {resolved.display} ใน watchlist"

    st = quick_status(removed, crypto_exchange=CRYPTO_EXCHANGE)
    pnl_txt = _fmt_pnl(st["pnl_pct"])

    # บันทึกลงสมุด (journal) + คำนวณ R
    r_txt = ""
    try:
        trade = wl_journal.record_trade(
            removed, st["current_price"], st["pnl_pct"],
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if trade.get("r_multiple") is not None:
            r_txt = f"  ({trade['r_multiple']:+.2f}R)"
    except Exception as e:  # noqa: BLE001
        log.warning("record_trade failed: %s", e)

    return (
        f"🗑️ ปิด {_SIDE_TH[side]}: {removed['display']} — เอาออกจาก watchlist แล้ว\n"
        f"เข้า {_fmt_price(removed.get('entry_price'))} → ตอนนี้ {_fmt_price(st['current_price'])}{pnl_txt}{r_txt}"
    )


def _handle_edit(args: list[str]) -> str:
    """แก้ราคาเข้าของ position: /edit SYMBOL ราคาใหม่"""
    if len(args) < 2:
        return "ใช้: /edit SYMBOL ราคาใหม่\nเช่น /edit SOL 160"
    try:
        resolved = resolve_symbol(args[0])
    except ValueError:
        return "❌ ไม่เข้าใจ symbol"
    try:
        new_price = float(args[1].replace(",", ""))
    except ValueError:
        return f"❌ ราคาไม่ถูกต้อง: {args[1]}"

    matches = store.get_by_ticker(resolved.data_ticker)
    if not matches:
        return f"⚠️ ไม่พบ {resolved.display} ใน watchlist"
    if len(matches) > 1:
        sides = ", ".join(_SIDE_TH.get(p["side"], p["side"]) for p in matches)
        return (f"⚠️ {resolved.display} มีหลายสถานะ ({sides}) — /edit แก้ได้เมื่อมีสถานะเดียว\n"
                f"ใช้ /sell|/callsell|/putsell ตัวที่ต้องการแล้วเพิ่มใหม่ด้วยราคาที่ถูก")

    pos = matches[0]
    old = pos.get("entry_price")
    pos["entry_price"] = new_price
    store.add_position(pos)
    return (f"✏️ แก้ราคาเข้า {pos['display']} [{_SIDE_TH[pos['side']]}]: "
            f"{_fmt_price(old)} → {_fmt_price(new_price)}")


def _handle_note(args: list[str]) -> str:
    """ใส่โน้ตให้ position: /note SYMBOL ข้อความ"""
    if len(args) < 2:
        return "ใช้: /note SYMBOL ข้อความ\nเช่น /note SOL รอ breakout 170"
    try:
        resolved = resolve_symbol(args[0])
    except ValueError:
        return "❌ ไม่เข้าใจ symbol"
    note = " ".join(args[1:]).strip()

    matches = store.get_by_ticker(resolved.data_ticker)
    if not matches:
        return f"⚠️ ไม่พบ {resolved.display} ใน watchlist"
    for pos in matches:
        pos["note"] = note
        store.add_position(pos)
    return f"📝 ใส่โน้ต {resolved.display}: {note}"


def _handle_macro() -> str:
    """ปฏิทินมาโคร US ที่กระทบสูง 7 วันข้างหน้า (กันซื้อ option ก่อนเหตุการณ์ใหญ่)"""
    try:
        from data.fundamentals import macro_warning, enabled
        if not enabled():
            return "ℹ️ ยังไม่ได้ตั้ง Finnhub API key (มาโครใช้ไม่ได้)"
        m = macro_warning(7)
        return m or "📅 7 วันข้างหน้า ไม่มีเหตุการณ์มาโคร US ที่กระทบสูง 🟢"
    except Exception as e:  # noqa: BLE001
        return f"❌ ดึงปฏิทินมาโครไม่สำเร็จ: {e}"


def _handle_news(args: list[str]) -> str:
    """ข่าวล่าสุด 24 ชม. ของหุ้น US (ดู/ทดสอบเองได้)
    /news        → ทุกตัวที่ถืออยู่ (US)
    /news SYMBOL → เจาะจงตัวเดียว (รองรับนอก watchlist)"""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        from data import fundamentals as fnd
        from watchlist import store
    except Exception as e:  # noqa: BLE001
        return f"❌ โมดูลข่าวไม่พร้อม: {e}"
    if not fnd.enabled():
        return "ℹ️ ยังไม่ได้ตั้ง Finnhub API key (ข่าวใช้ไม่ได้)"
    if args:
        syms = [args[0].upper().lstrip("$")]
    else:
        try:
            syms = sorted({(p.get("symbol") or "").upper() for p in store.list_positions()
                           if p.get("market") == "us" and p.get("symbol")})
        except Exception as e:  # noqa: BLE001
            return f"❌ โหลด watchlist ไม่สำเร็จ: {e}"
        if not syms:
            return "📰 ไม่มีหุ้น US ใน watchlist — ลอง /news SYMBOL เจาะจงได้"
    since = int(datetime.now(timezone.utc).timestamp()) - 24 * 3600
    th = ZoneInfo("Asia/Bangkok")
    # รวบรวมก่อน (top3/ตัว) แล้วแปลทีเดียว (batch กัน quota)
    groups: list[tuple[str, list]] = []
    flat: list[dict] = []
    for s in syms[:12]:
        items = sorted(fnd.news_items(s, since), key=lambda x: x["datetime"], reverse=True)[:3]
        if not items:
            continue
        groups.append((s, items))
        flat.extend({"id": it["id"], "headline": it["headline"]} for it in items)
    if not groups:
        return "📭 24 ชม.ที่ผ่านมาไม่มีข่าวของหุ้นที่ถือ (US)"
    th_map: dict = {}
    try:
        from data import translate as tr
        th_map = tr.to_thai(flat)
    except Exception:  # noqa: BLE001
        pass
    out: list[str] = []
    for s, items in groups:
        out.append(f"📌 {s}")
        for it in items:
            stamp = datetime.fromtimestamp(it["datetime"], tz=timezone.utc).astimezone(th).strftime("%d/%m %H:%M")
            show = th_map.get(it["id"]) or it["headline"]
            out.append(f"{fnd.news_direction(it['headline'])} {show}")
            meta = [it["source"]] if it.get("source") else []
            meta.append(stamp)
            out.append("   " + " · ".join(meta))
            if it.get("url"):
                out.append(f"   {it['url']}")
        out.append("")
    return "📰 ข่าวล่าสุด (24 ชม.)\n\n" + "\n".join(out).strip()


_ASK_SKIP = {"AI", "CEO", "ETF", "IPO", "USD", "GDP", "CPI", "FED", "PUT", "CALL",
             "BUY", "SELL", "AND", "THE", "FOR", "USA", "US", "ATH"}
_ZONE_LEGEND = ("green=ขาขึ้นแข็ง · yellow/orange=ขาขึ้นกำลังย่อ · "
                "red=ขาลง · lblue/blue=ขาลงกำลังเด้ง")


def _ask_symbol_context(token: str) -> "dict | None":
    """ดึงสแกนย่อของหุ้นที่ถูกถามถึง (ไม่ได้ถือ) → dict กระชับ ป้อน Gemini · None ถ้าไม่มีข้อมูล"""
    try:
        r = resolve_symbol(token)
    except Exception:  # noqa: BLE001
        return None
    try:
        df = fetch_history(r.market, r.data_ticker, crypto_exchange=CRYPTO_EXCHANGE)
        if df is None or df.empty:
            return None
        sig = compute_signal(df, r.data_ticker, display_name=r.display, score_when_none=True)
        if sig is None:
            sig = compute_signal(df, r.data_ticker, display_name=r.display,
                                 enable_ema200_filter=False, min_bars_required=30, score_when_none=True)
        if sig is None:
            return None
    except Exception:  # noqa: BLE001
        return None
    out = {"symbol": r.display, "market": r.market, "zone": sig.zone,
           "signal_today": sig.signal, "stars": sig.score,
           "rsi": round(sig.rsi) if sig.rsi is not None else None,
           "stage": sig.stage.get("label") if sig.stage else None,
           "price": sig.close}
    if r.market == "us":
        try:
            from data.fundamentals import fundamentals
            f = fundamentals(r.display)
            if f.get("analyst"):
                out["analyst"] = f["analyst"]
            if f.get("roe") is not None:
                out["roe_pct"] = round(f["roe"], 1) if isinstance(f["roe"], (int, float)) else f["roe"]
        except Exception:  # noqa: BLE001
            pass
    return out


def _handle_ask(args: list[str]) -> str:
    """ถาม-ตอบภาษาคน อิงข้อมูลจริง (พอร์ต + หุ้นที่ถามถึง + มาโคร) — by Gemini"""
    import re
    import json as _json
    q = " ".join(args).strip()
    if not q:
        return ("❓ พิมพ์คำถามต่อท้ายครับ เช่น\n"
                "• /ask พอร์ตฉันเสี่ยงตรงไหน\n"
                "• /ask ทำไม NVDA ถึงน่าถือ\n"
                "• /ask ตอนนี้ควรระวังอะไร")
    from data import ai
    if not ai.enabled():
        return "ℹ️ /ask ต้องใช้ Gemini (ยังไม่ได้ตั้ง GEMINI_API_KEY)"

    port: list[dict] = []
    held: set = set()
    try:
        for pos in store.list_positions():
            held.add((pos.get("symbol") or "").upper())
            st = full_status(pos, crypto_exchange=CRYPTO_EXCHANGE)
            stage = st.get("stage")
            pnl = st.get("pnl_pct")
            port.append({
                "symbol": pos.get("display") or pos.get("symbol"),
                "side": pos.get("side"),
                "zone": st.get("current_zone"),
                "stage": stage.get("label") if stage else None,
                "pnl_pct": round(pnl, 1) if pnl is not None else None,
                "exit_alert": bool(st.get("exit_alert")),
            })
    except Exception as e:  # noqa: BLE001
        log.warning("ask: โหลดพอร์ตล้มเหลว: %s", e)

    # หุ้นที่ถูกถามถึง (เฉพาะ token อังกฤษ, ไม่ได้ถือ, ไม่ใช่คำทั่วไป) — ดึงสแกนย่อ ≤2 ตัว
    extra: list[dict] = []
    for tok in dict.fromkeys(re.findall(r"[A-Za-z]{2,6}", q)):
        tu = tok.upper()
        if tu in held or tu in _ASK_SKIP:
            continue
        c = _ask_symbol_context(tu)
        if c:
            extra.append(c)
        if len(extra) >= 2:
            break

    macro = ""
    try:
        from data.fundamentals import macro_warning
        macro = macro_warning(7) or ""
    except Exception:  # noqa: BLE001
        pass

    prompt = (
        "คุณเป็นที่ปรึกษาการลงทุนที่ตอบโดย 'อิงข้อมูลจริงที่ให้เท่านั้น' ห้ามแต่งข้อมูล/ทำนายราคา "
        f"ความหมายโซน CDC: {_ZONE_LEGEND}\n"
        f"คำถามผู้ใช้: {q}\n"
        f"พอร์ตที่ผู้ใช้ถืออยู่: {_json.dumps(port, ensure_ascii=False) if port else 'ยังไม่มีหุ้นในพอร์ต'}\n"
        f"ข้อมูลหุ้นที่ถูกถามถึง: {_json.dumps(extra, ensure_ascii=False) if extra else 'ไม่มี/ไม่ได้ระบุ'}\n"
        f"ปฏิทินมาโคร US: {macro or 'ไม่มีเหตุการณ์เด่น'}\n\n"
        "ตอบเป็นไทย กระชับ ตรงคำถาม อิงเฉพาะข้อมูลข้างบน ถ้าข้อมูลไม่พอให้บอกตรง ๆ ว่าไม่มีข้อมูลพอ "
        "ใช้ข้อความธรรมดา + อิโมจิ (ห้าม markdown **). ปิดท้ายบรรทัดเดียว: ℹ️ ไม่ใช่คำแนะนำลงทุน"
    )
    ans = ai.gemini(prompt, temperature=0.4)
    if not ans or not str(ans).strip():
        return "❌ ตอบไม่ได้ตอนนี้ ลองใหม่อีกครั้งครับ"
    return "🤖 " + str(ans).strip()


def _handle_sectors() -> str:
    """J) Sector rotation — เงินไหลเข้า/ออกกลุ่มไหน (sector ETF เทียบ SPY) + AI สรุป"""
    from data.sectors import sector_rotation
    rows = sector_rotation()
    if not rows:
        return "❌ ดึงข้อมูล sector ไม่ได้ตอนนี้ ลองใหม่อีกครั้งครับ"
    lines = ["🔄 เงินไหลเข้าธีมไหน (Sector Rotation)",
             "เรียงตามโมเมนตัม 1 เดือน · เทียบ SPY", ""]
    for r in rows:
        mark = "•" if r["rs_1m"] is None else ("🟢" if r["rs_1m"] > 0 else "🔴")
        r1 = f"{r['ret_1m']:+.1f}%" if r["ret_1m"] is not None else "—"
        rs = f"{r['rs_1m']:+.1f}%" if r["rs_1m"] is not None else "—"
        lines.append(f"{mark} {r['name']} ({r['etf']}) — 1ด {r1} · vs SPY {rs}")
    from data import ai
    if ai.enabled():
        import json as _json
        prompt = (
            "จากข้อมูล sector rotation (ผลตอบแทน ETF กลุ่มอุตสาหกรรม US เทียบ SPY) สรุปเป็นไทย 2-3 บรรทัด: "
            "เงินไหลเข้ากลุ่มไหน (แข็ง) ออกจากกลุ่มไหน (อ่อน) และสะท้อนภาวะตลาดแบบ risk-on หรือ risk-off "
            "อิงข้อมูลเท่านั้น ห้ามแต่ง ใช้ข้อความธรรมดา (ห้ามใช้ ** หรือ markdown):\n" + _json.dumps(rows, ensure_ascii=False)
        )
        out = ai.gemini(prompt, temperature=0.3)
        if out and str(out).strip():
            lines += ["", "🧭 " + str(out).strip()]
    lines.append("\nℹ️ ไม่ใช่คำแนะนำลงทุน")
    return "\n".join(lines)


def _handle_thesis(args: list[str]) -> str:
    """K) ทบทวน thesis การถือหุ้น — รวมเทคนิค (zone/stage/RS) + ข่าวล่าสุด → AI ประเมินถือต่อไหม"""
    if not args:
        return "❓ พิมพ์ /thesis SYMBOL เพื่อทบทวนเหตุผลการถือ เช่น /thesis NVDA"
    from data import ai
    if not ai.enabled():
        return "ℹ️ /thesis ต้องใช้ Gemini (ยังไม่ได้ตั้ง GEMINI_API_KEY)"
    sym = args[0].upper().lstrip("$")
    pos = next((p for p in store.list_positions()
                if (p.get("symbol") or "").upper() == sym or (p.get("display") or "").upper() == sym), None)
    ctx = _ask_symbol_context(sym)
    if not ctx and not pos:
        return f"❌ ไม่พบข้อมูล {sym} (ลองระบุตลาด เช่น /scan {sym} us ดูก่อน)"

    news_summ: list[dict] = []
    try:
        r = resolve_symbol(sym)
        if r.market == "us":
            from data.fundamentals import news_items
            from datetime import datetime, timezone
            since = int(datetime.now(timezone.utc).timestamp()) - 24 * 3600
            items = news_items(r.data_ticker, since)[:8]
            if items:
                amap = ai.analyze_news([{"id": x["id"], "symbol": sym,
                                         "headline": x["headline"], "summary": x.get("summary", "")} for x in items])
                seen: set = set()
                for x in items:
                    a = amap.get(x["id"])
                    if a and a.get("relevant") and a.get("cluster") not in seen:
                        seen.add(a.get("cluster"))
                        news_summ.append({"th": a["th"], "dir": a["dir"]})
    except Exception as e:  # noqa: BLE001
        log.warning("thesis news %s failed: %s", sym, e)

    import json as _json
    pos_info = None
    if pos:
        try:
            st = full_status(pos, crypto_exchange=CRYPTO_EXCHANGE)
            pos_info = {"side": pos.get("side"), "entry": pos.get("entry_price"),
                        "pnl_pct": st.get("pnl_pct"), "exit_alert": st.get("exit_alert")}
        except Exception:  # noqa: BLE001
            pass
    prompt = (
        f"ทบทวน thesis การถือหุ้น {sym} จากข้อมูลจริงต่อไปนี้:\n"
        f"การถือ: {_json.dumps(pos_info, ensure_ascii=False) if pos_info else 'ไม่ได้ถืออยู่ (ประเมินภาพรวม)'}\n"
        f"เทคนิค: {_json.dumps(ctx, ensure_ascii=False) if ctx else 'ไม่มี'}\n"
        f"ข่าว 24 ชม.: {_json.dumps(news_summ, ensure_ascii=False) if news_summ else 'ไม่มีข่าวเด่น'}\n\n"
        "ประเมินเป็นไทยว่า 'เหตุผลที่จะถือยังอยู่ไหม' โดยรวมเทคนิค (zone/stage/RS) + ข่าว "
        "ขึ้นต้นด้วยป้ายสรุป 1 บรรทัด: ✅ thesis ยังแข็ง / ⚠️ เริ่มสั่นคลอน / 🔴 thesis เสีย ควรพิจารณาออก "
        "ตามด้วยเหตุผล 2-3 บรรทัด อิงข้อมูลเท่านั้น ห้ามแต่ง ใช้ข้อความธรรมดา (ห้ามใช้ ** หรือ markdown) "
        "ปิดท้าย: ℹ️ ไม่ใช่คำแนะนำลงทุน"
    )
    out = ai.gemini(prompt, temperature=0.3)
    if not out or not str(out).strip():
        return "❌ ประเมินไม่ได้ตอนนี้ ลองใหม่อีกครั้งครับ"
    return f"🔎 ทบทวน thesis: {sym}\n\n" + str(out).strip()


def _handle_tune() -> str:
    """L) วิเคราะห์ไม้ที่ปิดย้อนหลัง → AI เสนอจุดปรับกลยุทธ์ (เชิงคำแนะนำ ไม่ auto)"""
    from data import ai
    if not ai.enabled():
        return "ℹ️ /tune ต้องใช้ Gemini (ยังไม่ได้ตั้ง GEMINI_API_KEY)"
    trades = wl_journal.list_trades()
    if len(trades) < 5:
        return f"📒 ไม้ที่ปิดยังน้อย ({len(trades)} ไม้) — /tune ต้องการ ≥5 ไม้เพื่อหา pattern ที่เชื่อถือได้"
    stats = wl_journal.compute_stats() or {}
    import json as _json
    compact = [{"symbol": t.get("display") or t.get("symbol"), "side": t.get("side"),
                "pnl_pct": t.get("pnl_pct"), "r": t.get("r_multiple")} for t in trades[-40:]]
    prompt = (
        "คุณเป็นโค้ชเทรดที่วิเคราะห์ผลย้อนหลังเพื่อหา pattern จากไม้ที่ปิดแล้ว + สถิติต่อไปนี้ "
        "หา 'จุดที่ควรปรับ' (เช่น side ไหนแพ้บ่อย, สัญลักษณ์ที่ขาดทุนซ้ำ, R เฉลี่ยติดลบ, ปล่อยขาดทุนยาว) "
        "แล้วเสนอแนวทางปรับเชิงคำแนะนำ (รวมถึงพารามิเตอร์ CDC เช่น min RS / ADX / Stage filter ถ้าเหมาะ) "
        "อิงข้อมูลเท่านั้น ห้ามแต่งตัวเลข ใช้ข้อความธรรมดา (ห้ามใช้ ** หรือ markdown):\n"
        f"สถิติ: {_json.dumps(stats, ensure_ascii=False)}\n"
        f"ไม้ที่ปิด (ล่าสุด ≤40): {_json.dumps(compact, ensure_ascii=False)}\n\n"
        "โครงสร้าง: 1) 📊 อ่านผลรวม 2) 🔍 pattern ที่เจอ 3) 🔧 ข้อเสนอปรับ "
        "ปิดท้ายบรรทัดเดียว: ℹ️ คำแนะนำประกอบ ควรทดสอบก่อนใช้จริง"
    )
    out = ai.gemini(prompt, temperature=0.4)
    if not out or not str(out).strip():
        return "❌ วิเคราะห์ไม่ได้ตอนนี้ ลองใหม่อีกครั้งครับ"
    return "🛠️ ปรับกลยุทธ์จากสถิติ (วิเคราะห์โดย AI)\n\n" + str(out).strip()


def _handle_stats() -> str:
    stats = wl_journal.compute_stats()
    if not stats:
        return "📒 ยังไม่มีไม้ที่ปิด — สถิติจะขึ้นหลังคุณ /sell ไม้แรก"
    lines = [
        "📈 สถิติการเทรด (จากไม้ที่ปิดแล้ว)",
        f"• จำนวนไม้: {stats['trades']}",
    ]
    if stats["win_rate"] is not None:
        lines.append(f"• Win rate: {stats['win_rate']:g}%")
    if stats["avg_r"] is not None:
        lines.append(f"• ค่าเฉลี่ย R (expectancy): {stats['avg_r']:+.2f}R/ไม้")
        lines.append(f"• R รวม: {stats['total_r']:+.2f}R")
        lines.append(f"• ดีสุด/แย่สุด: {stats['best_r']:+.2f}R / {stats['worst_r']:+.2f}R")
    if stats["avg_pnl_pct"] is not None:
        lines.append(f"• กำไร/ขาดทุนเฉลี่ย: {stats['avg_pnl_pct']:+.2f}%")
    if stats.get("by_zone"):  # D3: win-rate ต่อโซนที่เข้า
        lines.append("• ตามโซนที่เข้า:")
        for z, zs in sorted(stats["by_zone"].items(), key=lambda kv: -kv[1]["n"]):
            from core.signals import zone_label
            lines.append(f"    {zone_label(z)}: {zs['win_rate']:.0f}% (n={zs['n']})")
    n = stats["trades"]
    avg = stats["avg_r"] or 0
    if n < 20:
        note = f"ℹ️ ตัวอย่างยังน้อย (n={n}) — ยังสรุป 'มี edge' ไม่ได้ · ดู /calib (วัดสัญญาณตรง ๆ)"
    elif avg > 0:
        note = (f"✅ ค่าเฉลี่ย R เป็นบวก — แต่มาจากไม้ที่คุณเลือกเข้า/ปิดเอง (selection bias)\n"
                "   วัด edge ของ 'สัญญาณ' แบบไม่ลำเอียงด้วย /calib")
    else:
        note = "⚠️ ค่าเฉลี่ย R ติดลบ — ทบทวนวินัย/กลยุทธ์"
    lines.append(note)
    return "\n".join(lines)


def _handle_calib() -> str:
    """D2: win-rate เชิงประจักษ์ของสัญญาณ CDC (mechanical forward-test จาก signals_log)"""
    try:
        import signals_log
        return signals_log.calib_summary()
    except Exception as e:  # noqa: BLE001
        log.warning("calib failed: %s", e)
        return "อ่าน calibration ไม่ได้ตอนนี้"


def _handle_scan(args: list[str]) -> str:
    """
    /scan                     → ทั้ง 4 กลุ่ม (สั่ง job, ผลตามมา ~1-2 นาที)
    /scan crypto|usstocks|... → เฉพาะกลุ่ม (สั่ง job)
    /scan SYMBOL              → เช็กตัวเดียว ตอบทันที (เดาตลาดให้)
    /scan <กลุ่ม> SYMBOL      → เช็กตัวเดียว บังคับตลาด (รองรับหุ้นนอก universe)
    /scan SYMBOL <กลุ่ม>      → สลับตำแหน่งได้ (เช่น /scan TSM us)
    """
    if not args:
        return _trigger_group_scan(None)

    key0 = _SCAN_ALIASES.get(args[0].lower())

    # กลุ่มล้วน (ไม่มี symbol) → สแกนทั้งกลุ่มผ่าน job
    if key0 and len(args) == 1:
        return _trigger_group_scan(key0)

    if len(args) >= 2:
        key1 = _SCAN_ALIASES.get(args[1].lower())
        # บังคับตลาด — ระบุกลุ่มตำแหน่งไหนก็ได้: "us TSM" หรือ "TSM us"
        if key0:
            return _single_scan_reply(_resolve_forced(key0, args[1]), args[1])
        if key1:
            return _single_scan_reply(_resolve_forced(key1, args[0]), args[0])

    # symbol ล้วน → เดาตลาดเอง (ถ้าเดาเป็น crypto ไม่เจอ ลองเป็นหุ้น US ให้)
    try:
        resolved = resolve_symbol(args[0])
    except ValueError:
        return "❌ ไม่เข้าใจ symbol"
    return _single_scan_reply(resolved, args[0], allow_us_fallback=("/" not in args[0]))


def _trigger_group_scan(group: "str | None") -> str:
    try:
        ok = _trigger_scan_job(group)
    except Exception as e:  # noqa: BLE001
        log.exception("scan trigger error: %s", e)
        return f"❌ สั่งสแกนไม่สำเร็จ: {e}"
    if ok:
        label = _SCAN_LABEL.get(group, "ทั้ง 4 กลุ่ม")
        return f"🔍 เริ่มสแกน {label} แล้ว — ผลจะส่งเข้ามาใน ~1-2 นาที"
    return "❌ สั่งสแกนไม่สำเร็จ — เช็ก log / สิทธิ์ run.invoker ของ service account"


def _single_scan_reply(resolved: Resolved, raw: str, *, allow_us_fallback: bool = False) -> str:
    try:
        txt = _scan_symbol(resolved)
        # เดาเป็น crypto แต่ไม่เจอข้อมูล → ลองเป็นหุ้น US (เผื่อหุ้นนอก universe)
        if txt is None and allow_us_fallback and resolved.market == "crypto":
            sym = raw.strip().upper()
            txt = _scan_symbol(Resolved("us", sym, sym))
    except Exception as e:  # noqa: BLE001
        log.exception("single scan error for %s: %s", raw, e)
        return f"❌ สแกน {raw} ไม่สำเร็จ: {e}"
    if txt is None:
        return (f"❌ ไม่พบข้อมูล '{raw}' — ลองระบุตลาดให้ชัด:\n"
                f"/scan us SYMBOL · /scan thai SYMBOL · /scan crypto SYMBOL")
    return txt


# โซน → ชื่อสั้น (อีโมจิ + สี) สำหรับ /list
_ZONE_SHORT = {
    "green": "🟢 เขียว", "yellow": "🟡 เหลือง", "orange": "🟠 เหลืองเข้ม",
    "red": "🔴 แดง", "lblue": "🩵 ฟ้าอ่อน", "blue": "🔵 ฟ้าเข้ม",
}
# ทิศกราฟตอนนี้ ตรง/สวน กับที่เปิดไว้ (มุมมองของ position) — bullish=spot/call, bearish=put
_VERDICT_BULL = {
    "green": "ตรงทาง ✅", "yellow": "เริ่มย่อ ⚡ เฝ้าดู", "orange": "ย่อแรง ⚡⚡ ใกล้กลับตัว",
    "red": "สวนทาง 🚩 พิจารณาปิด", "lblue": "สวนทาง ⚠️", "blue": "สวนทาง ⚠️",
}
_VERDICT_BEAR = {
    "red": "ตรงทาง ✅", "lblue": "เริ่มเด้ง ⚡ เฝ้าดู", "blue": "เด้งแรง ⚡⚡ ใกล้กลับตัว",
    "green": "สวนทาง 🚩 พิจารณาปิด", "yellow": "สวนทาง ⚠️", "orange": "สวนทาง ⚠️",
}


def _fmt_list_px(p) -> str:
    """ราคาในรูปแบบอ่านง่ายสำหรับ /list — ทศนิยม 2 ตำแหน่ง (เลขเล็กกว่า 1 ใช้ 4 หลักสำคัญ)"""
    if p is None:
        return "—"
    return f"{p:,.2f}" if abs(p) >= 1 else f"{p:.4g}"


def _thesis_verdict(side: str, zone: "str | None") -> str:
    if not zone:
        return ""
    table = _VERDICT_BULL if side in ("spot", "call") else _VERDICT_BEAR
    return table.get(zone, "")


def _position_block(pos: dict) -> str:
    """สร้างบล็อกข้อมูล 1 position สำหรับ /list (รันขนานกันได้ — full_status อ่านอย่างเดียว)"""
    st = full_status(pos, crypto_exchange=CRYPTO_EXCHANGE)
    side = pos["side"]
    is_option = side in ("call", "put")
    type_label = _SIDE_TH[side]
    if is_option and pos.get("strike"):
        stk = pos["strike"]
        type_label += f" {int(stk)}" if float(stk).is_integer() else f" {_fmt_list_px(stk)}"
    if pos.get("opt_expiry"):  # ติดตาม premium จริง → โชว์วันหมดอายุในชื่อ
        try:
            _, em, ed = str(pos["opt_expiry"]).split("-")
            type_label += f" (exp {ed}/{em})"
        except Exception:  # noqa: BLE001
            pass

    lines = [f"• {pos['display']} — {type_label}"]

    # premium จริง (EOD จาก Massive) — บรรทัดหลักของ option ที่ติดตาม
    if pos.get("opt_ticker") and pos.get("entry_premium"):
        try:
            from data import massive
            cur_prem = massive.premium(pos["opt_ticker"])
            ep = float(pos["entry_premium"])
            if cur_prem is not None and ep > 0:
                ppl = (cur_prem - ep) / ep * 100.0
                tag = "✅ กำไร" if ppl >= 0 else "🔻 ขาดทุน"
                lines.append(f"   💵 premium จริง: ${ep:g} → ${cur_prem:g}  {tag} {ppl:+.1f}%")
        except Exception as e:  # noqa: BLE001
            log.warning("list premium failed %s: %s", pos.get("opt_ticker"), e)

    # กำไร/ขาดทุน (ชัด ๆ ด้วยคำ) + การเคลื่อนของราคา
    pnl = st.get("pnl_pct")
    ent, cur = _fmt_list_px(pos.get("entry_price")), _fmt_list_px(st.get("current_price"))
    move = f"หุ้น {ent} → {cur}" if is_option else f"{ent} → {cur}"
    if pnl is None:
        lines.append(f"   {move}")
    elif pnl >= 0:
        lines.append(f"   ✅ กำไร +{pnl:.2f}%   ({move})")
    else:
        lines.append(f"   🔻 ขาดทุน {pnl:.2f}%   ({move})")

    # โซนกราฟ + ตรง/สวนทางกับที่เปิด (หัวใจของความเข้าใจ)
    zone = st.get("current_zone")
    if zone:
        label = "กราฟหุ้น" if is_option else "โซนกราฟ"
        lines.append(f"   {label}: {_ZONE_SHORT.get(zone, zone)} · {_thesis_verdict(side, zone)}")

    # ภาพใหญ่เทรนด์ (Weinstein Stage) + เตือนถ้าถือสวนเทรนด์ใหญ่
    stage = st.get("stage")
    if stage:
        bullish = side in ("spot", "call")
        warn = ""
        if (bullish and stage["n"] == 4) or (side == "put" and stage["n"] == 2):
            warn = " ⚠️ ถือสวนเทรนด์ใหญ่"
        lines.append(f"   {stage['emoji']} {stage['label']}{warn}")

    # เตือนเด่น ๆ (แตะ SL/TP)
    if st.get("sl_hit"):
        lines.append("   🚨 " + ("thesis เสีย — พิจารณาปิด!" if is_option else "หลุดจุดตัดขาดทุน — พิจารณาปิด!"))
    elif st.get("tp_level"):
        lines.append(f"   🎉 ถึงเป้า TP{st['tp_level']} แล้ว!")

    # จุดออก
    if pos.get("sl") is not None:
        sl, tp = _fmt_list_px(pos["sl"]), _fmt_list_px(pos.get("tp2"))
        if is_option:
            lines.append(f"   🛑 ปิดถ้าหุ้นถึง {sl}   🎯 เป้าหุ้น {tp}")
        else:
            lines.append(f"   🛑 ตัดขาดทุน {sl}   🎯 เป้า {tp}")

    # มุม option: เตือนงบ/IV crush + เบี้ยถูก-แพง (ใช้ df ที่ full_status ดึงมาแล้ว)
    try:
        from data.market import option_note_for_position
        onote = option_note_for_position(pos["market"], pos["symbol"], side, df=st.get("df"))
        if onote:
            lines.append(f"   💵 {onote}")
    except Exception as e:  # noqa: BLE001
        log.warning("list option note failed for %s: %s", pos.get("symbol"), e)

    # สภาพคล่อง option — สำคัญสำหรับของที่ "ถืออยู่" (ตอนนี้ยังขายออกไหม) เฉพาะ Call/Put US
    if _OPT_LIQ_ON and is_option and pos["market"] == "us":
        try:
            from data.market import option_liquidity
            from watchlist.tracker import recommended_min_dte
            dte = recommended_min_dte(pos.get("atr"), pos.get("adx"))  # expiry ที่แนะนำ
            liq = option_liquidity(pos["symbol"], st.get("current_price"), target_dte=dte, min_oi=_MIN_OI, max_spread_pct=_MAX_SPREAD)
            ex = _liq_exp(liq)
            ex_txt = f", exp {ex}" if ex else ""
            from main import _cached_txt
            cnote = _cached_txt(liq)  # " · 🕒 ข้อมูล ณ ..." ถ้ามาจาก cache
            stt = liq.get("status")
            if stt == "poor":
                oi = liq.get("oi")
                sp = liq.get("spread_pct")
                det = f"OI {oi:,}" + (f", spread {sp:.0f}%" if sp is not None else "")
                lines.append(f"   🔁 สภาพ option ไม่คล่อง ⛔ ({det}{ex_txt}) — ขายต่อยาก ระวังติด!{cnote}")
            elif stt == "good":
                lines.append(f"   🔁 สภาพ option คล่อง ✅ (OI {liq.get('oi'):,}{ex_txt}){cnote}")
            elif liq.get("reason") == "empty":
                lines.append("   🔁 ข้อมูล option ว่างตอนนี้ (yahoo รีเฟรชช่วง US ปิดดึก) — ดูในโบรก")
            else:  # none / unknown-fetch
                lines.append("   🔁 เช็กสภาพคล่อง option ไม่ได้ตอนนี้ — ดูในโบรก")
        except Exception as e:  # noqa: BLE001
            log.warning("list liquidity failed for %s: %s", pos.get("symbol"), e)

    if pos.get("note"):
        lines.append(f"   📝 {pos['note']}")
    return "\n".join(lines)


def _handle_list() -> str:
    positions = store.list_positions()
    if not positions:
        return "📭 watchlist ว่าง — เพิ่มด้วย /buy, /callbuy, /putbuy"

    # ดึงข้อมูลแต่ละ position ขนานกัน (กัน /list ช้าเมื่อเช็กสภาพคล่อง option หลายตัว)
    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(max_workers=6) as ex:
            blocks = list(ex.map(_position_block, positions))
    except Exception as e:  # noqa: BLE001 — เธรดพังให้ fallback ทำทีละตัว
        log.warning("list parallel failed: %s — fallback sequential", e)
        blocks = [_position_block(p) for p in positions]

    header = f"📊 Watchlist ({len(positions)} รายการ)"
    footer = ("─────────\n"
              "ℹ️ ✅ตรงทาง = กราฟไปทางที่เปิดไว้ · 🚩สวนทาง = พิจารณาปิด · 🔁 = สภาพคล่อง option\n"
              "%กำไร/ขาดทุนของ Call/Put คิดจากทิศหุ้นอ้างอิง ไม่ใช่ราคา premium จริง")
    return header + "\n\n" + "\n\n".join(blocks) + "\n\n" + footer


_HELP = (
    "🤖 CDC Watchlist Bot\n\n"
    "เปิดสถานะ:\n"
    "• /buy SYM [ตลาด] [ราคา] — ซื้อ Spot\n"
    "• /callbuy SYM [ตลาด] strike [วันหมดอายุ] [premium] — เปิด Call\n"
    "• /putbuy SYM [ตลาด] strike [วันหมดอายุ] [premium] — เปิด Put\n"
    "   ใส่วันหมดอายุ → ติดตาม premium จริง เช่น /putbuy LDOS 115 17/07 4.20\n"
    "   ระบุตลาดกัน resolve ผิด (หุ้น US ไปโดน crypto): us / thai / crypto / commodity\n"
    "   เช่น /buy SOFI us · /callbuy LDOS us 115 17/07\n\n"
    "ปิดสถานะ (เอาออกจาก watchlist):\n"
    "• /sell SYM [ตลาด] — ปิด Spot\n"
    "• /callsell SYM [ตลาด] — ปิด Call\n"
    "• /putsell SYM [ตลาด] — ปิด Put\n\n"
    "จัดการ:\n"
    "• /list — ดูทุกตัว + %P/L + โซน + SL/TP\n"
    "• /scan — สแกนทั้ง 4 กลุ่ม | /scan crypto|usstocks|thaistocks|commodity\n"
    "• /scan SYM — เช็กตัวเดียว ตอบทันที (เช่น /scan AAPL, /scan BTC, /scan PTT.BK)\n"
    "   นอก universe ระบุตลาด: /scan us SOFI · /scan thai XYZ\n"
    "• /edit SYM ราคา — แก้ราคาเข้า\n"
    "• /note SYM ข้อความ — ใส่โน้ต\n\n"
    "สถิติ/ความรู้:\n"
    "• /stats — สถิติ win-rate / R / expectancy (จากไม้ที่คุณปิด)\n"
    "• /calib — 🔮 win-rate ของ 'สัญญาณ' จริง (forward-test ไม่ลำเอียง)\n"
    "• /zone — ความหมาย 6 โซน CDC\n"
    "• /macro — ปฏิทินมาโคร US (FOMC/CPI/จ้างงาน) 7 วันข้างหน้า\n"
    "• /news [SYMBOL] — ข่าวล่าสุด 24 ชม. (บอตเตือนข่าวด่วนหุ้นที่ถืออัตโนมัติทุก ~10 นาที)\n"
    "• /ask <คำถาม> — ถาม AI อิงข้อมูลพอร์ตจริง เช่น /ask พอร์ตเสี่ยงตรงไหน\n"
    "• /sectors — เงินไหลเข้าธีมไหน (sector rotation)\n"
    "• /thesis SYMBOL — ทบทวนเหตุผลถือหุ้น (เทคนิค+ข่าว)\n"
    "• /tune — AI วิเคราะห์ไม้ที่ปิด เสนอจุดปรับกลยุทธ์\n\n"
    "Symbol: SOL, AAPL, CPALL, XAUUSD (ทอง), XAGUSD (เงิน), XCUUSD (ทองแดง)\n"
    "ไม่ใส่ราคา = บอตใช้ราคาตลาดล่าสุดให้\n"
    "เปิดสถานะแล้วบอตแนะนำ SL/TP อัตโนมัติ (จาก ATR) + เตือนเมื่อราคาแตะ"
)


_ZONE = (
    "🎨 CDC Action Zone V3 — 6 โซน\n"
    "EMA12 (fast) เทียบ EMA26 (slow) + ราคา (close)\n\n"
    "📈 ขาขึ้น (EMA12 > EMA26):\n"
    "🟢 เขียว = ราคา > EMA12 → ขาขึ้นแข็งแรง (ถือ/ซื้อ Call)\n"
    "🟡 เหลือง = หลุด EMA12 แต่ยังเหนือ EMA26 → เริ่มย่อ (ระวัง)\n"
    "🟠 เหลืองเข้ม = หลุดทั้ง EMA12+26 → ย่อแรง ปลายขาขึ้น (ใกล้กลับ)\n\n"
    "📉 ขาลง (EMA12 < EMA26):\n"
    "🔴 แดง = ราคา < EMA12 → ขาลงแข็งแรง (ถือ/ซื้อ Put)\n"
    "🩵 ฟ้าอ่อน = ขึ้นเหนือ EMA12 แต่ยังใต้ EMA26 → เริ่มเด้ง (ระวัง)\n"
    "🔵 ฟ้าเข้ม = ขึ้นเหนือทั้ง EMA12+26 → เด้งแรง ปลายขาลง (ใกล้กลับ)\n\n"
    "📍 สัญญาณ: 🟢 เขียวแรก = Buy · 🔴 แดงแรก = Sell\n"
    "💡 สีเข้ม (🟠 เหลืองเข้ม / 🔵 ฟ้าเข้ม) = ปลายเทรนด์ ระวังกลับตัว\n"
    "(แท่งปิดรายวัน — ไม่ repaint)"
)


def _dispatch(text: str) -> str:
    parts = text.strip().split()
    if not parts:
        return ""
    cmd = parts[0].lstrip("/").split("@")[0].lower()  # /buy@MyBot → buy
    args = parts[1:]

    if cmd in _OPEN:
        return _handle_open(cmd, args)
    if cmd in _CLOSE:
        return _handle_close(cmd, args)
    if cmd == "list":
        return _handle_list()
    if cmd == "scan":
        return _handle_scan(args)
    if cmd == "edit":
        return _handle_edit(args)
    if cmd == "note":
        return _handle_note(args)
    if cmd == "stats":
        return _handle_stats()
    if cmd == "calib":
        return _handle_calib()
    if cmd == "zone":
        return _ZONE
    if cmd == "macro":
        return _handle_macro()
    if cmd == "news":
        return _handle_news(args)
    if cmd == "ask":
        return _handle_ask(args)
    if cmd == "sectors":
        return _handle_sectors()
    if cmd == "thesis":
        return _handle_thesis(args)
    if cmd == "tune":
        return _handle_tune()
    if cmd in ("help", "start"):
        return _HELP
    return "ไม่รู้จักคำสั่งนี้ — พิมพ์ /help"


@app.get("/")
def health() -> tuple[str, int]:
    return "ok", 200


@app.get("/signals")
def signals_feed():
    """สะพาน Part 2 (MT5): คืนสัญญาณ CDC ล่าสุด (JSON) — กันด้วย token (?key= หรือ header)"""
    token = os.getenv("SIGNALS_TOKEN", "").strip() or WEBHOOK_SECRET
    if token:
        got = request.args.get("key", "") or request.headers.get("X-Signals-Token", "")
        if got != token:
            return {"error": "forbidden"}, 403
    try:
        return store.load_json("signals_latest.json", {"signals": [], "count": 0}), 200
    except Exception as e:  # noqa: BLE001
        log.warning("signals feed failed: %s", e)
        return {"error": "unavailable"}, 503


@app.post("/webhook")
def webhook() -> tuple[str, int]:
    # ตรวจ secret token ของ Telegram
    if WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != WEBHOOK_SECRET:
            log.warning("webhook: bad secret token")
            return "forbidden", 403

    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str((msg.get("chat") or {}).get("id", ""))

    if not text:
        return "ok", 200
    # ตอบเฉพาะเจ้าของ
    if CHAT_ID and chat_id != CHAT_ID:
        log.warning("webhook: ignore message from chat %s", chat_id)
        return "ok", 200

    try:
        reply = _dispatch(text)
    except Exception as e:  # noqa: BLE001 — ไม่ให้ webhook พังทั้งตัว
        log.exception("dispatch error: %s", e)
        reply = f"❌ เกิดข้อผิดพลาด: {e}"

    if reply:
        _reply(reply)
    return "ok", 200


if __name__ == "__main__":
    # dev only — โปรดักชันใช้ gunicorn
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
