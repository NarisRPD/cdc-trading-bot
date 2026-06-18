"""
main.py — orchestrator
- รัน 4 กลุ่ม: crypto, US stocks, Thai stocks, commodities
- หาสัญญาณ CDC V3 (Buy/Sell ครั้งแรก) + confluence score
- มัดรวมเป็นข้อความเดียวต่อกลุ่ม → ส่ง Telegram
- error ของกลุ่มเดียว ไม่ทำให้กลุ่มอื่นล่ม
"""
from __future__ import annotations
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

from config import Config, load_config
from core.signals import Signal, compute_signal, zone_label
from data.commodities import COMMODITIES, fetch_commodities
from data.crypto import fetch_crypto_universe, fetch_ohlcv_daily
from data.stocks import fetch_stocks_batch
from notify.telegram import send_telegram as _send_telegram_raw
from universe.set100 import get_set100_tickers, strip_bk_suffix
from universe.sp500 import get_sp500_tickers
from universe.nasdaq100 import get_nasdaq100_tickers
from universe.sp600 import get_sp600_tickers

# ─── logging ──────────────────────────────────────────────────────────
# บังคับ stdout เป็น UTF-8 — กัน Windows console (cp1252) crash เวลา log emoji/ลูกศร
# บน Cloud Run (Linux) เป็น UTF-8 อยู่แล้ว → no-op
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:  # noqa: BLE001
    pass

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("main")


# ─── ปิดแจ้งเตือนเชิงรุกวันเสาร์-อาทิตย์ (คำขอผู้ใช้ · อ้างอิงเวลาไทย) ──────────
# การส่ง Telegram ทุกชนิดใน main.py (สแกน CDC · ข่าว · โซน · บรีฟเช้า · รีวิวสัปดาห์)
# ผ่าน send_telegram ตัวนี้จุดเดียว → เงียบครบทุกอย่างในวันหยุดสุดสัปดาห์
# คุมด้วย env ALERTS_SKIP_WEEKEND (ดีฟอลต์เปิด) · ตั้ง false เพื่อกลับมาแจ้งทุกวัน
# ไม่กระทบ bot.py (ตอบคำสั่งที่ผู้ใช้พิมพ์เอง) และ signals_export (สะพาน Part 2 ยังทำงาน)
def _is_weekend_bkk() -> bool:
    """True ถ้าตอนนี้เป็นเสาร์(5)/อาทิตย์(6) ตามเวลาไทย (Asia/Bangkok)"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Bangkok")).weekday() >= 5


def send_telegram(msg: str, **kwargs) -> bool:
    """ห่อ sender จริง: ข้ามการส่งวันเสาร์-อาทิตย์ · วันธรรมดาส่งปกติ"""
    if (os.getenv("ALERTS_SKIP_WEEKEND", "true").strip().lower() in ("1", "true", "yes", "on")
            and _is_weekend_bkk()):
        log.info("ข้ามแจ้งเตือน — เสาร์/อาทิตย์ (ALERTS_SKIP_WEEKEND) · %.50s",
                 (msg or "").replace("\n", " "))
        return True   # ถือว่าสำเร็จ — ตัวเรียกจะได้ไม่นับ fail/ไม่ retry
    return _send_telegram_raw(msg, **kwargs)


@dataclass
class GroupResult:
    group_name: str       # "Crypto" / "US Stocks" / ...
    scanned: int
    skipped: int
    buy: List[Signal]
    sell: List[Signal]
    bar_date: pd.Timestamp | None
    regime_note: str = ""  # สถานะตลาดรวม (ดัชนีเหนือ/ใต้ EMA200)
    reversal: List[Signal] = field(default_factory=list)  # หุ้นโซน 🔵/🟠 ใกล้กลับตัว
    stale: int = 0         # จำนวนตัวที่ตัดเพราะข้อมูลค้าง (B1)
    fetch_failed: int = 0  # จำนวนตัวที่ดึงข้อมูลไม่ได้ (B2 — แยกจาก "ไม่มีสัญญาณ")
    universe: int = 0      # ขนาด universe ที่ขอดึง (B2 — คำนวณสัดส่วน blind)


# ─── per-group runners ────────────────────────────────────────────────

# ระยะเวลา (วันเทรด) สำหรับคำนวณ Relative Strength แบบถ่วงน้ำหนัก (เน้นช่วงสั้นกว่า)
_RS_WINDOWS = [(63, 0.5), (126, 0.3), (252, 0.2)]  # 3 / 6 / 12 เดือน
_RS_MIN_UNIVERSE = 20  # กลุ่มเล็กกว่านี้ไม่จัดอันดับ (เช่น commodities) — ไม่มีความหมาย


def _attach_relative_strength(
    signals: List[Signal], items: Dict[str, pd.DataFrame], cfg: Config
) -> None:
    """
    คำนวณ Relative Strength แบบกองทุน momentum: ผลตอบแทนถ่วงน้ำหนัก 3/6/12 เดือน
    แล้วจัดเป็น percentile (0-100) เทียบหุ้นทั้งกลุ่ม → เขียนกลับลง Signal.rs_rank
    (rs_rank=80 = แข็งกว่า 80% ของกลุ่ม) ข้ามถ้ากลุ่มเล็กไป
    """
    if not cfg.enable_rs:
        return
    raw: Dict[str, float] = {}
    for sym, df in items.items():
        try:
            close = df["close"].astype(float).dropna()
        except Exception:  # noqa: BLE001
            continue
        if len(close) < _RS_WINDOWS[0][0] + 5:  # ข้อมูลสั้นไป (ไม่ถึง ~3 เดือน)
            continue
        last = float(close.iloc[-1])
        if last <= 0:
            continue
        num = den = 0.0
        for win, weight in _RS_WINDOWS:
            if len(close) > win:
                past = float(close.iloc[-1 - win])
                if past > 0:
                    num += weight * (last / past - 1.0)
                    den += weight
        if den > 0:
            raw[sym] = num / den
    if len(raw) < _RS_MIN_UNIVERSE:
        return
    ordered = sorted(raw.items(), key=lambda kv: kv[1])  # อ่อน→แข็ง
    n = len(ordered)
    pct = {sym: round(100.0 * i / (n - 1)) for i, (sym, _) in enumerate(ordered)}
    for s in signals:
        if s.symbol in pct:
            s.rs_rank = float(pct[s.symbol])


def _compute_for(df, sym: str, name: str, cfg: Config, **extra):
    """เรียก compute_signal ด้วยพารามิเตอร์จาก cfg (ลดการเขียนซ้ำ)"""
    return compute_signal(
        df, symbol=sym, display_name=name,
        ema_fast=cfg.ema_fast, ema_slow=cfg.ema_slow, ema_trend=cfg.ema_trend,
        adx_period=cfg.adx_period, rsi_period=cfg.rsi_period,
        vol_sma_period=cfg.vol_sma_period,
        enable_ema200_filter=cfg.enable_ema200_filter,
        min_bars_required=cfg.min_bars_required,
        enable_mtf=cfg.enable_mtf, **extra,
    )


def _pick_reversal(signals: List[Signal], cfg: Config) -> List[Signal]:
    """หุ้นโซน 🔵/🟠 ใกล้กลับตัว — ถ้า reversal_fresh_only เอาเฉพาะ "เพิ่งเข้าโซน"
    (แท่งก่อนหน้าไม่ได้อยู่ในโซนกลับตัว) กันเตือนตัวเดิมซ้ำทุกวัน (C4)"""
    rev = [s for s in signals if s.zone in ("blue", "orange")]
    if cfg.reversal_fresh_only:
        # "เพิ่งเข้าโซนนี้" = แท่งก่อนหน้าอยู่คนละโซน (เทียบโซนตัวเอง ไม่ใช่เซ็ตรวม)
        # → ยอมรับการพลิกข้ามทิศ orange→blue / blue→orange (เป็นการเข้าโซนใหม่จริง ไม่ใช่ค้างเดิม)
        rev = [s for s in rev if (s.prev_zone or "") != s.zone]
    return rev


def _eval_dataframes(
    items: Dict[str, pd.DataFrame],
    cfg: Config,
    display_name_fn: Callable[[str], str] = lambda s: s,
    stale_days: Optional[int] = None,
) -> Tuple[List[Signal], int, int, int]:
    """รัน compute_signal ทั่ว dict; คืน (สัญญาณ, ok, skipped, stale) + ติด RS rank
    stale_days: ถ้า bar_date เก่ากว่านี้ (วัน) = ข้อมูลค้าง → ตัดทิ้ง (B1) · None = ไม่เช็ก"""
    signals: List[Signal] = []
    ok = skipped = stale = 0
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    for sym, df in items.items():
        sig = _compute_for(df, sym, display_name_fn(sym), cfg)
        if sig is None:
            skipped += 1
            log.debug("skip %s — data not sufficient", sym)
            continue
        if stale_days is not None and sig.bar_date is not None:
            try:
                age = (today - pd.Timestamp(sig.bar_date).normalize()).days
            except Exception:  # noqa: BLE001
                age = 0
            if age > stale_days:
                stale += 1
                log.warning("skip %s — ข้อมูลค้าง %d วัน (เกิน %d) bar=%s",
                            sym, age, stale_days, sig.bar_date.date())
                continue
        ok += 1
        signals.append(sig)
    _attach_relative_strength(signals, items, cfg)
    return signals, ok, skipped, stale


def _enrich_reversal(
    reversal: List[Signal], items: Dict[str, pd.DataFrame], cfg: Config
) -> List[Signal]:
    """
    เติม breakdown/แนะนำ/เป้า ให้ candidate ใกล้กลับตัว — ประเมินตาม "ทิศที่ดักกลับ"
    (🔵 → ประเมินแบบ Buy, 🟠 → ประเมินแบบ Sell) ไม่ใช่ทิศเทรนด์ปัจจุบัน
    enrich เฉพาะ top RS ต่อทิศ (เท่าที่จะโชว์จริง) — ที่เหลือคงไว้ดิบ ๆ (ไม่ถูกแสดงอยู่แล้ว)
    """
    def _by_rs(x: Signal) -> float:
        return -(x.rs_rank if x.rs_rank is not None else 50.0)

    ups = sorted([s for s in reversal if s.zone == "blue"], key=_by_rs)
    downs = sorted([s for s in reversal if s.zone == "orange"], key=_by_rs)
    to_enrich = {id(s) for s in ups[: cfg.reversal_max] + downs[: cfg.reversal_max]}

    out: List[Signal] = []
    for s in reversal:
        if id(s) not in to_enrich:  # RS ต่ำ ไม่อยู่ใน top → ไม่ต้องคำนวณซ้ำ
            out.append(s)
            continue
        direction = "buy" if s.zone == "blue" else "sell"  # 🔵 ดักขึ้น · 🟠 ดักลง
        df = items.get(s.symbol)
        rich = _compute_for(
            df, s.symbol, s.display_name, cfg,
            score_when_none=True, eval_direction=direction,
        ) if df is not None else None
        if rich is None:
            s.anticipate = direction
            out.append(s)
            continue
        rich.rs_rank = s.rs_rank  # คง RS ที่คำนวณไว้แล้ว
        out.append(rich)
    return out


# ─── market regime (ดัชนีอ้างอิงเหนือ EMA200 ไหม) ─────────────────────

def _regime_note(market: str, ticker: str, label: str) -> str:
    """สถานะตลาดรวม: ดัชนีอ้างอิงอยู่เหนือ/ใต้ EMA200 → คืนข้อความสั้น (ว่างถ้าดึงไม่ได้)"""
    try:
        from data.quote import fetch_history
        from core.indicators import ema
        df = fetch_history(market, ticker)
        if df is None or len(df) < 200:
            return ""
        close = df["close"].astype(float)
        e200 = ema(close, 200).iloc[-1]
        if pd.isna(e200):
            return ""
        bull = float(close.iloc[-1]) > float(e200)
        state = "เหนือ EMA200 ✅ ตลาดเอื้อ Long" if bull else "ใต้ EMA200 ⚠️ ตลาดอ่อน ระวัง Long"
        return f"🌍 {label}: {state}"
    except Exception as e:  # noqa: BLE001 — regime เป็นออปชัน พังก็ข้าม
        log.warning("regime note failed (%s %s): %s", market, ticker, e)
        return ""


def run_crypto(cfg: Config) -> GroupResult:
    log.info("=== Crypto ===")
    ex, symbols = fetch_crypto_universe(
        cfg.crypto_exchange, top_n=cfg.crypto_top_n,
        min_volume_usdt=cfg.crypto_min_volume_usdt,
    )
    if ex is None or not symbols:
        return GroupResult("Crypto", 0, 0, [], [], None)

    items: Dict[str, pd.DataFrame] = {}
    skipped_fetch = 0
    for sym in symbols:
        df = fetch_ohlcv_daily(ex, sym)
        if df is None:
            skipped_fetch += 1
            continue
        items[sym] = df

    signals, ok, skipped_signal, stale = _eval_dataframes(items, cfg, stale_days=cfg.max_stale_days_crypto)
    buys = [s for s in signals if s.signal == "buy"]
    sells = [s for s in signals if s.signal == "sell"]
    reversal = _pick_reversal(signals, cfg)  # ใกล้กลับตัว (C4: เฉพาะเพิ่งเข้าโซน)
    if cfg.enable_reversal_watch:
        reversal = _enrich_reversal(reversal, items, cfg)
    bar = max((s.bar_date for s in signals), default=None)
    log.info("Crypto: scanned=%d, skipped(fetch)=%d, skipped(data)=%d, stale=%d, buy=%d, sell=%d",
             ok, skipped_fetch, skipped_signal, stale, len(buys), len(sells))
    regime = _regime_note("crypto", "BTC/USDT", "Crypto (BTC)") if cfg.enable_regime else ""
    return GroupResult("Crypto", ok, skipped_fetch + skipped_signal + stale, buys, sells, bar, regime, reversal,
                       stale=stale, fetch_failed=skipped_fetch, universe=len(symbols))


def run_us_stocks(cfg: Config) -> GroupResult:
    log.info("=== US Stocks (S&P500 + NASDAQ100 + S&P600) ===")
    # รวม 3 ดัชนี dedup: S&P500 (ใหญ่) + NASDAQ-100 (เทค) + S&P600 (เล็กที่กำไรแล้ว)
    tickers = list(dict.fromkeys(
        get_sp500_tickers() + get_nasdaq100_tickers() + get_sp600_tickers()
    ))
    log.info("US universe รวม %d ตัว (dedup แล้ว)", len(tickers))
    items = fetch_stocks_batch(tickers, period="2y")
    signals, ok, skipped, stale = _eval_dataframes(items, cfg, stale_days=cfg.max_stale_days_equity)
    buys = [s for s in signals if s.signal == "buy"]
    sells = [s for s in signals if s.signal == "sell"]
    reversal = _pick_reversal(signals, cfg)  # ใกล้กลับตัว (C4: เฉพาะเพิ่งเข้าโซน)
    if cfg.enable_reversal_watch:
        reversal = _enrich_reversal(reversal, items, cfg)
    bar = max((s.bar_date for s in signals), default=None)
    fetch_failed = len(tickers) - len(items)
    log.info("US Stocks: scanned=%d, fetch_failed=%d, stale=%d, buy=%d, sell=%d",
             ok, fetch_failed, stale, len(buys), len(sells))
    regime = _regime_note("us", "^GSPC", "US (S&P500)") if cfg.enable_regime else ""
    return GroupResult("US Stocks", ok, len(tickers) - ok, buys, sells, bar, regime, reversal,
                       stale=stale, fetch_failed=fetch_failed, universe=len(tickers))


def run_thai_stocks(cfg: Config) -> GroupResult:
    log.info("=== Thai Stocks (SET100) ===")
    tickers = get_set100_tickers()
    items = fetch_stocks_batch(tickers, period="2y")
    signals, ok, skipped, stale = _eval_dataframes(
        items, cfg, display_name_fn=strip_bk_suffix, stale_days=cfg.max_stale_days_equity,
    )
    buys = [s for s in signals if s.signal == "buy"]
    sells = [s for s in signals if s.signal == "sell"]
    reversal = _pick_reversal(signals, cfg)  # ใกล้กลับตัว (C4: เฉพาะเพิ่งเข้าโซน)
    if cfg.enable_reversal_watch:
        reversal = _enrich_reversal(reversal, items, cfg)
    bar = max((s.bar_date for s in signals), default=None)
    fetch_failed = len(tickers) - len(items)
    log.info("Thai Stocks: scanned=%d, fetch_failed=%d, stale=%d, buy=%d, sell=%d",
             ok, fetch_failed, stale, len(buys), len(sells))
    regime = _regime_note("thai", "^SET.BK", "ไทย (SET)") if cfg.enable_regime else ""
    return GroupResult("Thai Stocks", ok, len(tickers) - ok, buys, sells, bar, regime, reversal,
                       stale=stale, fetch_failed=fetch_failed, universe=len(tickers))


def run_commodities(cfg: Config) -> GroupResult:
    log.info("=== Commodities ===")
    items = fetch_commodities()
    signals, ok, skipped, stale = _eval_dataframes(
        items, cfg, display_name_fn=lambda s: COMMODITIES.get(s, s), stale_days=cfg.max_stale_days_equity,
    )
    buys = [s for s in signals if s.signal == "buy"]
    sells = [s for s in signals if s.signal == "sell"]
    reversal = _pick_reversal(signals, cfg)  # ใกล้กลับตัว (C4: เฉพาะเพิ่งเข้าโซน)
    if cfg.enable_reversal_watch:
        reversal = _enrich_reversal(reversal, items, cfg)
    bar = max((s.bar_date for s in signals), default=None)
    fetch_failed = len(COMMODITIES) - len(items)
    log.info("Commodities: scanned=%d, fetch_failed=%d, stale=%d, buy=%d, sell=%d",
             ok, fetch_failed, stale, len(buys), len(sells))
    regime = _regime_note("commodity", "GC=F", "ทองคำ") if cfg.enable_regime else ""
    return GroupResult("Commodities", ok, len(COMMODITIES) - ok, buys, sells, bar, regime, reversal,
                       stale=stale, fetch_failed=fetch_failed, universe=len(COMMODITIES))


# ─── message formatting ───────────────────────────────────────────────

def _time_hint(s: Signal) -> str:
    """คาดคะเนเวลาถึงเป้า + วันหมดอายุ option รายตัว (lazy import กันพัง)"""
    try:
        from watchlist.tracker import time_to_target_hint
        return time_to_target_hint(s.atr, s.adx)
    except Exception:  # noqa: BLE001
        return ""


def _fmt_px(v: float) -> str:
    """ฟอร์แมตราคาให้อ่านง่ายตามขนาด"""
    if v is None:
        return "—"
    if abs(v) >= 100:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:,.4f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


def _liq_exp(liq: dict | None) -> str:
    """วันหมดอายุของ expiry ที่ใช้เช็กสภาพคล่อง (DD/MM) — ตรงกับ DTE ที่แนะนำในบรรทัด ⏱️"""
    e = liq.get("expiry") if liq else None
    if not e:
        return ""
    try:
        return pd.Timestamp(e).strftime("%d/%m")
    except Exception:  # noqa: BLE001
        return ""


def _cached_txt(liq: dict | None) -> str:
    """ป้าย 'ข้อมูล ณ ...' (เวลาไทย) เมื่อค่าสภาพคล่องมาจาก cache (yahoo ว่างตอนนี้)"""
    if not liq or not liq.get("cached"):
        return ""
    try:
        d = pd.Timestamp(liq["cached_ts"]).tz_convert("Asia/Bangkok")
        return f" · 🕒 ข้อมูล ณ {d.strftime('%d/%m %H:%M')}"
    except Exception:  # noqa: BLE001
        return " · 🕒 (จาก cache)"


def _option_guidance_from_liq(liq: dict | None) -> str:
    """
    บรรทัด option แยกจากบรรทัดทิศ (👉) — แสดงเฉพาะหุ้นที่เช็ก option แล้ว (= US):
      good    → 🎟️ สัญญาแนะนำ (วันหมดอายุ + คล่อง)
      poor    → ⛔ option ไม่คล่อง (เตือนขายต่อยาก)
      unknown → ℹ️ ไม่มีข้อมูล
    คืน "" ถ้าไม่ใช่ US / ไม่ได้เช็ก (ไทย/คริปโต/ทอง → ไม่มี option)
    """
    if not liq:
        return ""
    st = liq.get("status")
    oi = liq.get("oi")
    sp = liq.get("spread_pct")
    ex = _liq_exp(liq)  # DD/MM
    cached = _cached_txt(liq)  # " · 🕒 ข้อมูล ณ ..." ถ้าเป็นค่าจาก cache (yahoo ว่างตอนนี้)
    if st == "good" and liq.get("expiry"):
        try:
            d = pd.Timestamp(liq["expiry"])
            dte = (d - pd.Timestamp.now()).days
            date_txt = d.strftime("%d/%m/%Y")
        except Exception:  # noqa: BLE001
            return ""
        oi_txt = f" · ✅ คล่อง (OI {oi:,})" if oi else ""
        return f"🎟️ สัญญาแนะนำ: ซื้อ option หมดอายุ ~{date_txt} (~{dte} วัน){oi_txt}{cached}"
    if st == "poor":
        det = f"OI {oi:,}" + (f", spread {sp:.0f}%" if sp is not None else "")
        exq = f"เช็ก exp {ex}: " if ex else ""
        return f"⛔ สภาพ option ไม่คล่อง ({exq}{det}) — ขายต่อยาก/โดน spread กิน · เล่นหุ้นจริงแทน{cached}"
    if st == "none":
        return "🚫 หุ้นนี้ไม่มี option ให้เทรด — เทรดได้เฉพาะหุ้นจริง"
    if liq.get("reason") == "empty":
        return ("ℹ️ ข้อมูล option ว่างตอนนี้ (yahoo มักว่างช่วงตลาด US ปิดดึก ≈ เที่ยง-บ่ายไทย) — "
                "เช็กช่วงตลาด US เปิด (กลางคืนไทย) หรือดูในโบรก")
    return "ℹ️ ดึงข้อมูล option ไม่ได้ — เช็กในโบรกเองก่อนซื้อ"


def _option_guidance(s: Signal) -> str:
    return _option_guidance_from_liq(s.option_liq)


def _recommendation(s: Signal, direction: str | None = None, reversal: bool = False) -> str:
    """คำแนะนำทิศ Call/Put + ความเชื่อมั่น (เป็นสัญญาณ CDC ล้วน — สภาพคล่องแยกไป _option_guidance)"""
    d = direction or s.signal
    if d == "buy":
        action = "Call (เก็งราคาขึ้น)"
    elif d == "sell":
        action = "Put (เก็งราคาลง)"
    else:
        return ""
    if s.high_quality and s.mtf_aligned is True:
        conf = "สัญญาณแข็งแรง น่าเข้า"
    elif s.high_quality:
        conf = "ดี แต่ weekly ยังไม่ยืนยัน — ไม้เล็ก/รอ confirm"
    elif (s.score or 0) >= 2:
        conf = "ปานกลาง — รอ confirm หรือไม้เล็ก"
    else:
        conf = "อ่อน — แนะนำรอ"
    if reversal:
        return f"👉 ถ้า CDC ยืนยันกลับตัว → เปิด {action} ({conf})"
    return f"👉 แนะนำ: เปิด {action} ({conf})"


def _projection(s: Signal, direction: str | None = None) -> list[str]:
    """คาดการณ์เป้าราคา (อิง ATR = ระยะที่ราคามักวิ่งจริง) — ไม่ใช่คำทำนายแน่นอน"""
    d = direction or s.signal
    if not s.atr or not s.close or d not in ("buy", "sell"):
        return []
    atr, close = s.atr, s.close

    def pct(p: float) -> str:
        return f"{(p - close) / close * 100:+.1f}%"

    if d == "buy":
        near, far, inval = close + 1.5 * atr, close + 3.0 * atr, close - 1.5 * atr
        return [
            f"🎯 คาดเป้าขึ้น: {_fmt_px(near)} ({pct(near)}) → {_fmt_px(far)} ({pct(far)})",
            f"🛡️ ถ้าหลุด: {_fmt_px(inval)} ({pct(inval)}) = สัญญาณเริ่มเสีย",
        ]
    near, far, inval = close - 1.5 * atr, close - 3.0 * atr, close + 1.5 * atr
    return [
        f"🎯 คาดเป้าลง: {_fmt_px(near)} ({pct(near)}) → {_fmt_px(far)} ({pct(far)})",
        f"🛡️ ถ้าเด้งเหนือ: {_fmt_px(inval)} ({pct(inval)}) = สัญญาณเริ่มเสีย",
    ]


def _transition_line(s: Signal) -> str:
    """สีแท่งที่เพิ่งทำ (🟢 Buy / 🔴 Sell) + ความหมายแบบเป็นธรรมชาติ"""
    prev = s.prev_zone
    if s.signal == "none" or not prev:
        return ""
    if s.signal == "buy":  # เพิ่งทำเขียว
        if prev in ("yellow", "orange"):       # ขาขึ้นย่อแล้วกลับขึ้น = ไปต่อ
            meaning = "กราฟขึ้นต่อเนื่อง เป็นขาขึ้น"
        elif prev in ("lblue", "blue"):        # ขาลงเด้งจนพลิกขึ้น = กลับตัว
            meaning = "กราฟกำลังกลับตัวขึ้น (reversal)"
        else:  # red                            # พลิกจากขาลงแรงเป็นขึ้นในแท่งเดียว
            meaning = "กราฟกลับตัวขึ้นแรงจากขาลง (reversal)"
        return f"🟢 {meaning}"
    # sell, เพิ่งทำแดง
    if prev in ("lblue", "blue"):              # ขาลงเด้งแล้วลงต่อ = ไปต่อ
        meaning = "กราฟลงต่อเนื่อง เป็นขาลง"
    elif prev in ("yellow", "orange"):         # ขาขึ้นย่อจนพลิกลง = กลับตัว
        meaning = "กราฟกำลังกลับตัวลง (reversal)"
    else:  # green                              # พลิกจากขาขึ้นแรงเป็นลงในแท่งเดียว
        meaning = "กราฟกลับตัวลงแรงจากขาขึ้น (reversal)"
    return f"🔴 {meaning}"


def _stage_line(s: Signal) -> Optional[str]:
    """บรรทัด Weinstein Stage (ภาพใหญ่เทรนด์ — เสริม CDC zone) · None ถ้าข้อมูลไม่พอ"""
    st = s.stage
    if not st:
        return None
    return f"{st['emoji']} {st['label']} · {st['note']}"


def _trend_quality_line(s: Signal) -> Optional[str]:
    """ป้ายคุณภาพเทรนด์ (R²): เนียน vs ขรุขระ — ยังไม่ตัด แค่บอกให้เห็นค่าจริง"""
    tq = s.trend_q
    if not tq or tq.get("r2") is None:
        return None
    r2 = tq["r2"]
    if r2 >= 0.8:
        return f"📏 เทรนด์สะอาด (R² {r2:.2f} — เนียน เป็นเส้น)"
    if r2 >= 0.5:
        return f"📏 เทรนด์พอใช้ (R² {r2:.2f})"
    return f"〰️ เทรนด์ขรุขระ (R² {r2:.2f} — แกว่ง/ยังไม่ชัด)"


def _bull_dir(s: Signal) -> Optional[bool]:
    """ทิศของไม้: สัญญาณจริง > ทิศที่ดักกลับตัว (reversal) > โซนปัจจุบัน
    คืน True=ขาขึ้น/Buy, False=ขาลง/Put, None=ไม่ชัด
    (กันเคส 'ใกล้กลับขึ้น' ที่โซนยังเป็นหมีแต่ทิศจริงคือรอ Buy)"""
    if s.signal == "buy":
        return True
    if s.signal == "sell":
        return False
    if s.anticipate == "buy":
        return True
    if s.anticipate == "sell":
        return False
    if s.zone in ("green", "yellow", "orange"):
        return True
    if s.zone in ("red", "lblue", "blue"):
        return False
    return None


def _ema_tangled(s: Signal) -> bool:
    """EMA12/26 'พันกัน' (ระยะห่าง < 0.5 ATR) = ยังไม่มีทิศ · ถ่างแล้ว = เริ่มมีเทรนด์
    บอกไม่ได้ (ไม่มีข้อมูล) → ถือว่าไม่พันกัน (เก็บไว้ กันตัดมั่ว)"""
    if not (s.ema_fast and s.ema_slow and s.close):
        return False
    gap = abs(s.ema_fast - s.ema_slow)
    if s.atr and s.atr > 0:
        return gap / s.atr < 0.5
    return gap / s.close < 0.005


def _is_sideway(s: Signal, adx_max: float = 20.0) -> bool:
    """sideway แบบสมดุล — ตัดเมื่อครบ 3: ADX ต่ำ AND MA150 แบน AND EMA12/26 พันกัน
    เก็บ early-trend ไว้ ถ้า 'MA150 เริ่มเงย' หรือ 'EMA เริ่มถ่าง' อย่างใดอย่างหนึ่ง"""
    adx = s.adx
    if adx is None or adx >= adx_max:
        return False  # ADX สูงพอ = มีเทรนด์
    st = s.stage
    if st is not None:
        slope = st.get("slope_pct") or 0.0
        if slope >= 1.0 or slope <= -1.0:  # MA150 เงยขึ้น/ลงชัด = early-trend → เก็บ
            return False
    # MA150 แบน (หรือแท่งไม่พอจน stage=None) → ดู EMA: ถ่างแล้ว = ต้นเทรนด์ → เก็บ
    if not _ema_tangled(s):
        return False
    return True  # ADX ต่ำ + MA150 แบน + EMA พันกัน = sideway จริง → ตัด


def _trend_position(s: Signal) -> Optional[str]:
    """ต้น/กลาง/ปลายเทรนด์ จากระยะยืดของราคาห่าง EMA12 (หน่วย ATR) + RSI — กันไล่ซื้อตอนยืด (ดอย)"""
    fast, close, atr = s.ema_fast, s.close, s.atr
    bull = _bull_dir(s)
    if fast is None or not close or not atr or atr <= 0 or bull is None:
        return None
    ext = abs(close - fast) / atr  # ยืดจาก EMA12 กี่ ATR
    rsi = s.rsi
    if bull:
        if ext >= 3.0 or (rsi is not None and rsi >= 70):
            extra = f" · RSI {rsi:.0f}" if rsi is not None and rsi >= 70 else ""
            return f"📍 ตำแหน่ง: ⚠️ ปลายเทรนด์ (ยืด {ext:.1f} ATR{extra}) — เสี่ยงดอย รอย่อ"
        if ext >= 1.5:
            return f"📍 ตำแหน่ง: 🔥 กลางเทรนด์ (ยืด {ext:.1f} ATR)"
        return f"📍 ตำแหน่ง: 🍃 ต้น-กลางเทรนด์ (ยืด {ext:.1f} ATR) — ไม่ยืดเกิน"
    # ฝั่งลง (Put)
    if ext >= 3.0 or (rsi is not None and rsi <= 30):
        extra = f" · RSI {rsi:.0f}" if rsi is not None and rsi <= 30 else ""
        return f"📍 ตำแหน่ง: ⚠️ ปลายขาลง (ยืด {ext:.1f} ATR{extra}) — เสี่ยงเด้ง รอเด้ง"
    if ext >= 1.5:
        return f"📍 ตำแหน่ง: 🔥 กลางขาลง (ยืด {ext:.1f} ATR)"
    return f"📍 ตำแหน่ง: 🍃 ต้น-กลางขาลง (ยืด {ext:.1f} ATR)"


def _entry_line(s: Signal) -> Optional[str]:
    """โซนราคาเข้าที่เหมาะจากเทคนิค (EMA12/EMA26 = แนวรับ/ต้าน) — เข้าตอนย่อแตะแนว ไม่ไล่ของแพง
    Buy → โซนเข้าซื้อ (แนวรับ) · Sell → โซนเข้า Put (แนวต้าน)"""
    fast, slow, close, atr = s.ema_fast, s.ema_slow, s.close, s.atr
    bull = _bull_dir(s)
    if fast is None or slow is None or not close or bull is None:
        return None
    lo, hi = (slow, fast) if fast >= slow else (fast, slow)  # แถบ EMA (รับ/ต้าน)
    buf = atr * 0.5 if atr else close * 0.01                  # ยอมรับว่า "อยู่ในโซน" ได้ ~ครึ่ง ATR
    band = f"{_fmt_px(lo)}–{_fmt_px(hi)}"
    if bull:
        if close <= hi + buf:
            return f"💰 โซนเข้าซื้อเหมาะ: {band} (แนวรับ EMA) · ตอนนี้ {_fmt_px(close)} อยู่ในโซน → ทยอยเข้าได้"
        return (f"💰 โซนเข้าซื้อเหมาะ: {band} (แนวรับ EMA) · ตอนนี้ {_fmt_px(close)} สูงกว่าโซน "
                f"→ รอย่อแตะ ~{_fmt_px(hi)} ค่อยเข้า (กันไล่แพง R:R แย่)")
    if close >= lo - buf:
        return f"💰 โซนเข้า Put เหมาะ: {band} (แนวต้าน EMA) · ตอนนี้ {_fmt_px(close)} อยู่ในโซน → ทยอยเข้าได้"
    return (f"💰 โซนเข้า Put เหมาะ: {band} (แนวต้าน EMA) · ตอนนี้ {_fmt_px(close)} ต่ำกว่าโซน "
            f"→ รอเด้งแตะ ~{_fmt_px(lo)} ค่อยเข้า")


def _format_signal_line(s: Signal, cfg: Config) -> str:
    # ดาวสูงสุด 5 = 4 confluence (Trend/ADX/Vol/RSI) + ยืนยันกราฟรายสัปดาห์
    star_count = s.score + (1 if s.mtf_aligned is True else 0)
    score_txt = f" {'⭐' * star_count}" if cfg.enable_filters else ""
    hq = " HQ" if (cfg.enable_filters and s.high_quality) else ""
    rs_txt = f" · RS {s.rs_rank:.0f}" if s.rs_rank is not None else ""  # ความแข็งเทียบกลุ่ม
    lines = [f"• {s.display_name} @ {_fmt_px(s.close)}{score_txt}{hq}{rs_txt}"]
    tl = _transition_line(s)
    if tl:
        lines.append(tl)
    sl = _stage_line(s)  # ภาพใหญ่เทรนด์ (Weinstein Stage)
    if sl:
        lines.append(sl)
    tq = _trend_quality_line(s)  # คุณภาพเทรนด์ (R² เนียน/ขรุขระ)
    if tq:
        lines.append(tq)
    if cfg.show_filter_breakdown and s.breakdown:
        for b in s.breakdown:
            lines.append(("✅ " if b["ok"] else "❌ ") + b["text"])
    rec = _recommendation(s)
    if rec:
        lines.append(rec)
    el = _entry_line(s)  # โซนราคาเข้าที่เหมาะ (แนวรับ/ต้าน EMA)
    if el:
        lines.append(el)
    tp = _trend_position(s)  # ต้น/กลาง/ปลายเทรนด์ (กันดอย)
    if tp:
        lines.append(tp)
    lines.extend(_projection(s))
    if rec:  # มีสัญญาณ Call/Put → คาดคะเนเวลาถึงเป้า + วันหมดอายุรายตัว
        th = _time_hint(s)
        if th:
            lines.append(th)
    g = _option_guidance(s)  # บรรทัด option (สัญญาแนะนำ/ไม่คล่อง/ไม่มีข้อมูล) เฉพาะหุ้น US
    if g:
        lines.append(g)
    if s.fund_flag:  # พื้นฐาน/นักวิเคราะห์ (Finnhub/FMP)
        lines.append(s.fund_flag)
    return "\n".join(lines)


def _pass_hard_filters(s: Signal, cfg: Config) -> bool:
    """ตัดทิ้งเลย: sideway (ADX ต่ำ) / ไม่มีแรง (vol ต่ำ) / Buy overbought / RS อ่อน"""
    if s.adx is not None and s.adx <= cfg.min_adx_to_alert:
        return False  # sideway
    if cfg.require_volume_above_sma and s.vol_above is False:
        return False  # ไม่มีแรงซื้อขาย
    if s.rsi is not None and s.signal == "buy" and s.rsi >= cfg.max_rsi_buy:
        return False  # overbought (ไล่ของแพง)
    # Relative Strength gate — มีผลเฉพาะกลุ่มใหญ่พอ (rs_rank ถูกคำนวณ)
    if cfg.enable_rs and s.rs_rank is not None:
        if s.signal == "buy" and cfg.min_rs_buy > 0 and s.rs_rank < cfg.min_rs_buy:
            return False  # อ่อนกว่าตลาด ไม่ใช่ leader → ไม่ซื้อ
        if s.signal == "sell" and cfg.max_rs_sell < 100 and s.rs_rank > cfg.max_rs_sell:
            return False  # แข็งเกินไป ไม่เหมาะ Put
    return True


def _filter_for_alert(sigs: List[Signal], cfg: Config) -> List[Signal]:
    out = sigs
    if cfg.alert_high_quality_only:
        # เหลือเฉพาะตัวน่าเข้าจริง: ≥3 ดาว + ผ่านเทรนด์ EMA200
        out = [s for s in out if s.high_quality]
    elif cfg.min_score_to_alert > 0:
        out = [s for s in out if s.score >= cfg.min_score_to_alert]
    if cfg.require_mtf:
        # ตัดเฉพาะที่สวนเทรนด์รายสัปดาห์ชัดเจน (mtf_aligned=False); None (ไม่รู้) ไม่ตัด
        out = [s for s in out if s.mtf_aligned is not False]
    out = [s for s in out if _pass_hard_filters(s, cfg)]  # ตัด sideway/overbought/no-volume
    return out


def _reversal_messages(r: GroupResult, cfg: Config, ref_date: str) -> List[str]:
    """
    "ใกล้กลับตัว" — แยกเป็นคนละข้อความ: 🔵 ใกล้กลับขึ้น / 🟠 ใกล้กลับลง (เหมือน CDC Buy/Sell แยกกัน)
    ดักก่อนสัญญาณจริง · ข้าม hard filters (ADX/Vol/RSI) · เรียงตาม RS + cap กันท่วม
    """
    def _by_rs(x: Signal) -> float:
        return -(x.rs_rank if x.rs_rank is not None else 50.0)

    # กรอง sideway (ADX ต่ำ + MA150 แบน) ออก — กันหมวดนี้รกด้วยตัวไร้เทรนด์ (เก็บ early-trend ไว้)
    sw = cfg.sideway_adx_max if cfg.filter_sideway else 0.0
    ups = sorted([s for s in r.reversal if s.zone == "blue" and not _is_sideway(s, sw)], key=_by_rs)
    downs = sorted([s for s in r.reversal if s.zone == "orange" and not _is_sideway(s, sw)], key=_by_rs)
    out: List[str] = []

    def _msg(header: str, sub: str, items: List[Signal]) -> None:
        if not items:
            return
        shown = items[: cfg.reversal_max]
        cap = (f"แสดง {len(shown)}/{len(items)} (เรียงตาม RS)"
               if len(items) > len(shown) else f"{len(items)} ตัว")
        parts = [f"{header} — {r.group_name} · {cap}", sub]
        if r.regime_note:
            parts.append(r.regime_note)
        for s in shown:
            parts.append("")
            parts.append(_reversal_block(s, cfg))
        parts.append("")
        parts.append(f"📅 อ้างอิงแท่งปิดวันที่: {ref_date}")
        parts.append("※ เฝ้าดู (ข้ามกฎ ADX/Vol/RSI) — ยังไม่ใช่สัญญาณ รอ CDC ยืนยันก่อนเข้า")
        out.append("\n".join(parts))

    _msg("🔵 ใกล้กลับขึ้น (รอ Buy)", "ปลายขาลง เด้งแรง · รอ EMA12 ตัดเหนือ EMA26", ups)
    _msg("🟠 ใกล้กลับลง (รอ Sell)", "ปลายขาขึ้น ย่อแรง · รอ EMA12 ตัดใต้ EMA26", downs)
    return out


def _reversal_block(s: Signal, cfg: Config) -> str:
    """บล็อกเต็มของ candidate ใกล้กลับตัว — breakdown + แนะนำ + เป้า (เหมือน /scan แต่กรอบ anticipation)"""
    rs = f" · RS {s.rs_rank:.0f}" if s.rs_rank is not None else ""
    lines = [f"• {s.display_name} @ {_fmt_px(s.close)}{rs}"]
    sl = _stage_line(s)  # ภาพใหญ่เทรนด์ (Weinstein Stage)
    if sl:
        lines.append(sl)
    tq = _trend_quality_line(s)  # คุณภาพเทรนด์ (R² เนียน/ขรุขระ)
    if tq:
        lines.append(tq)
    if cfg.show_filter_breakdown and s.breakdown:
        for b in s.breakdown:
            lines.append(("✅ " if b["ok"] else "❌ ") + b["text"])
    d = s.anticipate  # "buy"=ดักขึ้น · "sell"=ดักลง
    rec = _recommendation(s, direction=d)  # คำแนะนำตรง ๆ (header "รอ Buy/Sell" บอก context แล้ว)
    if rec:
        lines.append(rec)
    el = _entry_line(s)  # โซนราคาเข้าที่เหมาะ (แนวรับ/ต้าน EMA)
    if el:
        lines.append(el)
    tp = _trend_position(s)  # ต้น/กลาง/ปลายเทรนด์ (กันดอย)
    if tp:
        lines.append(tp)
    lines.extend(_projection(s, direction=d))
    th = _time_hint(s)
    if th:
        lines.append(th)
    g = _option_guidance(s)  # บรรทัด option (สัญญาแนะนำ/ไม่คล่อง/ไม่มีข้อมูล)
    if g:
        lines.append(g)
    return "\n".join(lines)


def _attach_option_liquidity(signals: List[Signal], cfg: Config) -> None:
    """
    เช็กสภาพคล่อง option (OI/spread) ของสัญญาณที่จะโชว์ — ดึง option chain ขนานกัน (US เท่านั้น)
    ดึงเฉพาะตัวที่โชว์จริง (~10-20 ตัว) ไม่ใช่ทั้ง 1,100 → ขนานแล้วเพิ่มแค่ ~5s
    """
    if not cfg.enable_option_liquidity or not signals:
        return
    from concurrent.futures import ThreadPoolExecutor
    from data.market import option_liquidity
    from watchlist.tracker import recommended_min_dte

    def _fetch(s: Signal) -> None:
        try:
            dte = recommended_min_dte(s.atr, s.adx)  # เช็ก expiry ที่ ⏱️ แนะนำ
            s.option_liq = option_liquidity(
                s.symbol, s.close, target_dte=dte,
                min_oi=cfg.min_option_oi, max_spread_pct=cfg.max_option_spread,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("option liquidity fetch failed %s: %s", s.symbol, e)

    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_fetch, signals))
    except Exception as e:  # noqa: BLE001
        log.warning("liquidity pool failed: %s", e)
    # อัปเดต cache เป็น batch (กัน race) — scan 07:00 รันช่วงเย็น US ได้ค่าจริง → /scan ตอนช่วงว่างใช้ cache
    from data.market import liq_cache_put_many
    liq_cache_put_many({s.symbol: s.option_liq for s in signals if s.option_liq})


def _attach_fundamentals(signals: List[Signal], cfg: Config) -> None:
    """ติด flag พื้นฐาน/นักวิเคราะห์ (Finnhub/FMP) — workers น้อย + cached กัน rate limit (US เท่านั้น)"""
    if not cfg.enable_fundamentals or not signals:
        return
    try:
        from data.fundamentals import fundamental_flag, enabled
    except Exception:  # noqa: BLE001
        return
    if not enabled():
        return
    from concurrent.futures import ThreadPoolExecutor

    def _fetch(s: Signal) -> None:
        try:
            s.fund_flag = fundamental_flag(s.symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("fundamentals fetch failed %s: %s", s.symbol, e)

    try:
        with ThreadPoolExecutor(max_workers=4) as ex:  # 4 workers กัน Finnhub 60/min
            list(ex.map(_fetch, signals))
    except Exception as e:  # noqa: BLE001
        log.warning("fundamentals pool failed: %s", e)


def _ai_top_picks(buys: List[Signal]) -> Optional[str]:
    """G) ให้ Gemini จัด Top 3 หุ้นน่าสนใจสุดจากสัญญาณ Buy ทั้งหมด + เหตุผลสั้น
    เลือกจากในรายการเท่านั้น (ground) · ทำเฉพาะเมื่อ Buy ≥ 4 ตัว (น้อยกว่านั้นไม่ต้องจัดอันดับ)"""
    try:
        from data import ai as _ai
    except Exception:  # noqa: BLE001
        return None
    if not _ai.enabled() or len(buys) < 4:
        return None
    import json as _json
    data = [{
        "symbol": s.display_name, "zone": s.zone, "stars": s.score,
        "rs_rank": round(s.rs_rank) if s.rs_rank is not None else None,
        "stage": s.stage.get("label") if s.stage else None,
        "rsi": round(s.rsi) if s.rsi is not None else None,
        "fundamentals": s.fund_flag,
    } for s in buys]
    prompt = (
        "จากรายการหุ้นที่เกิดสัญญาณซื้อ (CDC Buy) วันนี้ เลือก 'Top 3 ที่น่าสนใจที่สุด' "
        "ให้น้ำหนัก: ดาวเยอะ (stars 0-4), RS สูง (แข็งกว่าตลาด), Stage 2 (ขาขึ้นใหญ่), พื้นฐานดี "
        "ตอบเป็นไทย แต่ละตัวบรรทัดเดียวรูปแบบ 'N. SYMBOL — เหตุผลสั้น' "
        "เลือกจากในรายการเท่านั้น ห้ามแต่งเพิ่ม ใช้ข้อความธรรมดา (ห้าม markdown):\n"
        + _json.dumps(data, ensure_ascii=False)
    )
    out = _ai.gemini(prompt, temperature=0.3)
    if not out or not str(out).strip():
        return None
    return "🏆 น่าสนใจสุดวันนี้ (คัดโดย AI)\n" + str(out).strip()


def build_messages(results: List[GroupResult], cfg: Config) -> List[str]:
    """
    มัดรวมต่อกลุ่ม — buy + sell อยู่คนละ section
    คืน list ของข้อความ (กลุ่มที่ไม่มีสัญญาณจะถูกข้าม)
    """
    bar_dates = [r.bar_date for r in results if r.bar_date is not None]
    ref_date = max(bar_dates).strftime("%Y-%m-%d") if bar_dates else "—"

    def _section(title: str, sigs: List[Signal], subtitle: str = "") -> str:
        parts = [title]
        if subtitle:
            parts.append(subtitle)
        # เรียง: ยืนยัน weekly ก่อน → RS แข็งสุด (buy) / อ่อนสุด (sell) → ดาว
        is_buy = bool(sigs) and sigs[0].signal == "buy"

        def _rs_key(x: Signal) -> float:
            rs = x.rs_rank if x.rs_rank is not None else 50.0
            return -rs if is_buy else rs  # buy: แข็งขึ้นบน · sell: อ่อนขึ้นบน

        ordered = sorted(
            sigs,
            key=lambda x: (0 if x.mtf_aligned is True else 1, _rs_key(x), -x.score, x.display_name),
        )
        for s in ordered:
            parts.append("")  # เว้นบรรทัดระหว่างสัญญาณ ให้อ่านง่าย
            parts.append(_format_signal_line(s, cfg))
        parts.append("")
        parts.append(f"📅 อ้างอิงแท่งปิดวันที่: {ref_date}")
        parts.append("※ คำแนะนำเชิงกลไก ไม่ใช่คำแนะนำลงทุน · ปิด/โรล option ก่อนหมดอายุ ~2 สัปดาห์")
        return "\n".join(parts)

    messages: List[str] = []
    all_buys: List[Signal] = []  # G) รวม Buy ทุกกลุ่มไว้ให้ AI จัด Top 3
    for r in results:
        buys = _filter_for_alert(r.buy, cfg)
        sells = _filter_for_alert(r.sell, cfg)
        all_buys.extend(buys)
        # เช็กสภาพคล่อง option ก่อน format (เฉพาะหุ้น US — yahoo มี option chain) สำหรับตัวที่โชว์จริง
        if r.group_name == "US Stocks":
            shown_rev = [s for s in r.reversal if s.breakdown]  # ตัวที่ enrich = ที่จะโชว์
            _attach_option_liquidity(list(buys) + list(sells) + shown_rev, cfg)
            _attach_fundamentals(list(buys) + list(sells), cfg)  # flag พื้นฐาน (buy/sell — กัน rate)
        rev_msgs = _reversal_messages(r, cfg, ref_date) if cfg.enable_reversal_watch else []
        # diagnostic: ตัวเลขก่อน→หลังกรอง (พิสูจน์ว่าไม่มี cap — เป็นผลจาก filter จริง)
        log.info(
            "%s filter: buy %d→%d, sell %d→%d, reversal=%d  [HQ_only=%s, MIN_ADX=%.0f, REQ_VOL=%s, MAX_RSI_BUY=%.0f]",
            r.group_name, len(r.buy), len(buys), len(r.sell), len(sells), len(r.reversal),
            cfg.alert_high_quality_only, cfg.min_adx_to_alert,
            cfg.require_volume_above_sma, cfg.max_rsi_buy,
        )
        # แยก Buy / Sell เป็นคนละข้อความให้ชัด (+ สถานะตลาดรวมใต้หัวข้อ)
        if buys:
            messages.append(_section(f"🟢 CDC Buy — {r.group_name} ({len(buys)} ตัว)", buys, r.regime_note))
        if sells:
            messages.append(_section(f"🔴 CDC Sell — {r.group_name} ({len(sells)} ตัว)", sells, r.regime_note))
        messages.extend(rev_msgs)  # 🔵 ใกล้กลับขึ้น / 🟠 ใกล้กลับลง (คนละข้อความ)

    if not messages:
        # แจ้งสถานะแม้ไม่มีสัญญาณ — แยก "ดึงข้อมูลไม่ได้" ออกจาก "ไม่มีสัญญาณ" (B2)
        total_scanned = sum(r.scanned for r in results)
        total_skipped = sum(r.skipped for r in results)  # ข้ามทั้งหมด (ข้อมูลไม่พอ+ดึงไม่ได้+ค้าง)
        total_failed = sum(r.fetch_failed for r in results)
        total_stale = sum(r.stale for r in results)
        kind = "คุณภาพสูง (HIGH-QUALITY)" if cfg.alert_high_quality_only else "Buy/Sell ใหม่"
        extra = ""
        if total_failed:
            extra += f"\n⚠️ ในนั้นดึงข้อมูลไม่ได้ {total_failed} ตัว (อาจไม่ใช่ 'ตลาดเงียบจริง')"
        if total_stale:
            extra += f"\n⚠️ ข้อมูลค้างถูกตัด {total_stale} ตัว"
        messages.append(
            f"ℹ️ CDC Scanner — ไม่มีสัญญาณ{kind}ในวันนี้\n"
            f"สแกนได้ {total_scanned} ตัว / ข้าม {total_skipped} ตัว{extra}\n"
            f"📅 อ้างอิงแท่งปิดวันที่: {ref_date}"
        )

    # G) Top 3 น่าสนใจสุด (คัดโดย AI จาก Buy ทั้งหมด) — แทรกก่อน แล้วมาโครจะไปอยู่บนสุด
    tp = _ai_top_picks(all_buys)
    if tp:
        messages.insert(0, tp)

    # เตือนมาโคร US บนสุด (เฉพาะตอนสแกนมี US — กระทบทั้งตลาด/ก่อนซื้อ option)
    if cfg.enable_fundamentals and any(r.group_name == "US Stocks" for r in results):
        try:
            from data.fundamentals import macro_warning
            mw = macro_warning(5)
            if mw:
                messages.insert(0, mw)
        except Exception as e:  # noqa: BLE001
            log.warning("macro warning failed: %s", e)

    # B2) เตือน "ข้อมูลไม่ครบ" บนสุด — กันเข้าใจผิดว่า 'ตลาดเงียบ' ทั้งที่ดึงข้อมูลไม่ได้
    blind = [r for r in results if r.universe > 0 and r.fetch_failed / r.universe > 0.5]
    if blind:
        warn = "⚠️ ข้อมูลไม่ครบ — ดึงไม่ได้เกินครึ่ง ผลอาจไม่สมบูรณ์:\n" + "\n".join(
            f"• {r.group_name}: ดึงได้ {r.universe - r.fetch_failed}/{r.universe}" for r in blind
        )
        messages.insert(0, warn)
    return messages


# ─── watchlist report (อ่านรายการที่ถือ → รายงานสถานะ + เตือนสัญญาณปิด) ──

def build_watchlist_report(cfg: Config) -> str | None:
    """รายงานสถานะ position ที่ถืออยู่; คืน None ถ้า watchlist ว่าง/อ่านไม่ได้"""
    try:
        from watchlist import store
        from watchlist.tracker import full_status, apply_trailing
    except Exception as e:  # noqa: BLE001 — ไม่มี watchlist ก็ข้าม
        log.warning("watchlist module ไม่พร้อม: %s", e)
        return None

    try:
        positions = store.list_positions()
    except Exception as e:  # noqa: BLE001
        log.warning("โหลด watchlist ไม่สำเร็จ: %s", e)
        return None

    if not positions:
        return None

    side_th = {"spot": "Spot", "call": "Call", "put": "Put"}
    lines = ["📊 สถานะ Watchlist"]
    for pos in positions:
        try:
            st = full_status(pos, crypto_exchange=cfg.crypto_exchange)
        except Exception as e:  # noqa: BLE001
            log.warning("watchlist status %s ล้มเหลว: %s", pos.get("symbol"), e)
            continue
        # Trailing stop — เลื่อน SL ล็อกกำไรเป็นขั้น R แล้วเก็บลง store
        trail_note = ""
        new_sl = apply_trailing(pos, st["current_price"])
        if new_sl is not None:
            pos["sl"] = new_sl
            try:
                store.add_position(pos)
            except Exception as e:  # noqa: BLE001
                log.warning("เก็บ trailed SL ของ %s ไม่สำเร็จ: %s", pos.get("symbol"), e)
            entry_p = pos.get("entry_price")
            if entry_p and abs(new_sl - entry_p) / entry_p < 0.001:
                trail_note = "🔒 เลื่อน SL มาทุน (breakeven — ไม้นี้ไม่เสี่ยงต่อ)"
            else:
                trail_note = f"🔒 เลื่อน SL → {new_sl:.4g} (ล็อกกำไร)"

        pnl = st["pnl_pct"]
        pnl_txt = f" {pnl:+.2f}%" if pnl is not None else ""
        zone = zone_label(st["current_zone"])
        cur = f"{st['current_price']:.4g}" if st["current_price"] is not None else "—"
        entry = f"{pos['entry_price']:.4g}" if pos.get("entry_price") is not None else "—"
        is_option = pos["side"] in ("call", "put")
        label = side_th.get(pos["side"], pos["side"])
        if is_option and pos.get("strike") is not None:
            label += f" {pos['strike']:.4g}"
        u = "หุ้น " if is_option else ""
        line = (f"• {pos['display']} [{label}] {u}{entry}→{cur}{pnl_txt} | {zone}")
        if st.get("sl_hit"):
            line += " 🛑 thesis เสีย!" if is_option else " 🛑 หลุด SL!"
        elif st.get("tp_level"):
            line += f" 🎯 ถึงเป้า{st['tp_level']}!"
        if st["exit_alert"]:
            line += " ⚠️ พิจารณาปิด"
        if pos.get("sl") is not None:
            sl_s = f"{pos['sl']:.4g}"
            tp2_s = f"{pos['tp2']:.4g}" if pos.get("tp2") is not None else "—"
            line += (f"\n   หุ้น: ปิดถ้าทะลุ {sl_s} · เป้า {tp2_s}" if is_option
                     else f"\n   SL {sl_s} · TP {tp2_s}")
        if trail_note:
            line += f"\n   {trail_note}"
        if pos.get("note"):
            line += f"\n   📝 {pos['note']}"
        lines.append(line)

    log.info("watchlist report: %d position(s)", len(positions))
    return "\n".join(lines)


# map ชื่อหมวด → runner (ใช้กับ env SCAN_GROUPS สำหรับ /scan รายหมวด)
GROUP_RUNNERS = {
    "crypto": run_crypto,
    "usstocks": run_us_stocks,
    "thaistocks": run_thai_stocks,
    "commodity": run_commodities,
}


def _select_runners() -> tuple[list, bool]:
    """
    อ่าน env SCAN_GROUPS (comma-separated) → คืน (runners, is_full)
    ว่าง = สแกนทุกกลุ่ม (is_full=True)
    """
    raw = os.getenv("SCAN_GROUPS", "").strip().lower()
    if not raw:
        return list(GROUP_RUNNERS.values()), True
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    chosen = [GROUP_RUNNERS[k] for k in keys if k in GROUP_RUNNERS]
    if not chosen:
        log.warning("SCAN_GROUPS='%s' ไม่ตรงหมวดใด — สแกนทุกกลุ่มแทน", raw)
        return list(GROUP_RUNNERS.values()), True
    return chosen, len(chosen) == len(GROUP_RUNNERS)


def run_watchlist_alerts(cfg: Config) -> int:
    """
    โหมดเบา (WATCHLIST_ALERT_ONLY): เช็กเฉพาะตัวใน /list → เตือนเมื่อ "โซนเปลี่ยน"
    เทียบกับ last_zone ที่จำไว้ (กันซ้ำ) · เตือนเฉพาะเข้าโซนแดง/เขียว (เด็ด)
    """
    try:
        from watchlist import store
        from watchlist.tracker import full_status
    except Exception as e:  # noqa: BLE001
        log.warning("watchlist module ไม่พร้อม: %s", e)
        return 0
    try:
        positions = store.list_positions()
    except Exception as e:  # noqa: BLE001
        log.warning("โหลด watchlist ไม่สำเร็จ: %s", e)
        return 0
    if not positions:
        log.info("watchlist ว่าง — ไม่มีอะไรเช็ก")
        return 0

    side_th = {"spot": "Spot", "call": "Call", "put": "Put"}
    bullish = {"spot", "call"}
    changes: list[str] = []   # ตัวที่เปลี่ยนโซน (เตือน)
    snapshot: list[str] = []  # โซนปัจจุบันทุกตัว
    for pos in positions:
        try:
            st = full_status(pos, crypto_exchange=cfg.crypto_exchange)
        except Exception as e:  # noqa: BLE001
            log.warning("status %s ล้มเหลว: %s", pos.get("symbol"), e)
            continue
        cur = st["current_zone"]
        label = side_th.get(pos["side"], pos["side"])
        if pos["side"] in ("call", "put") and pos.get("strike") is not None:
            label += f" {pos['strike']:.4g}"
        snapshot.append(f"• {pos['display']} [{label}] {zone_label(cur)}")
        if not cur:
            continue
        last = pos.get("last_zone")
        if cur != last:
            pos["last_zone"] = cur
            try:
                store.add_position(pos)
            except Exception as e:  # noqa: BLE001
                log.warning("เก็บ last_zone %s ไม่สำเร็จ: %s", pos.get("symbol"), e)
            if last is None:
                continue  # ครั้งแรก = ตั้ง baseline เฉย ๆ ไม่เตือน
            # 3 ระดับ: ✅ เข้าทาง / ⚡ ระวัง (EMA ตัดสวนแล้วแต่ราคายังไม่ยืนยัน) / ⚠️ เสีย
            kind = None
            if pos["side"] in bullish:  # ถือ Spot/Call (อยากให้ขึ้น)
                if cur == "green":
                    kind = "✅ เข้าทาง — ขาขึ้น"
                elif cur == "yellow":
                    kind = "⚡ ระวัง — ขาขึ้นเริ่มย่อ (หลุด EMA12)"
                elif cur == "orange":
                    kind = "⚡ ระวังหนัก — ปลายขาขึ้น (ย่อหลุด EMA26)"
                elif cur in ("red", "lblue", "blue"):
                    kind = "⚠️ thesis เสีย — เทรนด์กลับเป็นขาลง ควรพิจารณาปิด"
            else:  # ถือ Put (อยากให้ลง)
                if cur == "red":
                    kind = "✅ เข้าทาง — ขาลง"
                elif cur == "lblue":
                    kind = "⚡ ระวัง — ขาลงเริ่มเด้ง (ขึ้นเหนือ EMA12)"
                elif cur == "blue":
                    kind = "⚡ ระวังหนัก — ปลายขาลง (เด้งเหนือ EMA26)"
                elif cur in ("green", "yellow", "orange"):
                    kind = "⚠️ thesis เสีย — เทรนด์กลับเป็นขาขึ้น ควรพิจารณาปิด"
            if kind is None:
                continue
            changes.append(f"{kind}\n   {pos['display']} [{label}] → {zone_label(cur)}")

    # warm cache สภาพคล่อง option ของ position (job นี้รัน 04:30/16:45 ไทย = ตลาด US มีข้อมูล)
    # → /list หรือ /scan ตอน yahoo ว่าง (เที่ยง-บ่ายไทย) จะดึง cache มาโชว์ได้
    if cfg.enable_option_liquidity:
        try:
            from data.market import option_liquidity, liq_cache_put_many
            from watchlist.tracker import recommended_min_dte
            warm = {}
            for pos in positions:
                if pos.get("side") in ("call", "put") and pos.get("market") == "us":
                    dte = recommended_min_dte(pos.get("atr"), pos.get("adx"))
                    warm[pos["symbol"]] = option_liquidity(
                        pos["symbol"], pos.get("entry_price"), target_dte=dte,
                        min_oi=cfg.min_option_oi, max_spread_pct=cfg.max_option_spread,
                    )
            liq_cache_put_many(warm)
        except Exception as e:  # noqa: BLE001
            log.warning("warm liquidity cache failed: %s", e)

    # warm premium cache (Massive) ของ position ที่ติดตาม premium จริง → /list เร็ว + กัน 5 calls/min
    try:
        from data import massive
        if massive.enabled():
            prem = {}
            for pos in positions:
                tk = pos.get("opt_ticker")
                if tk:
                    p = massive._premium_live(tk)
                    if p is not None:
                        prem[tk] = p
            massive.prem_cache_put_many(prem)
    except Exception as e:  # noqa: BLE001
        log.warning("warm premium cache failed: %s", e)

    if not changes:
        log.info("watchlist: ไม่มีโซนเปลี่ยน (เช็ก %d ตัว) — ไม่ส่ง", len(positions))
        return 0

    msg = ("🔔 CDC อัปเดตโซน Watchlist\n\n" + "\n\n".join(changes)
           + "\n\n📊 โซนปัจจุบันทุกตัว:\n" + "\n".join(snapshot))
    if cfg.dry_run:
        log.info("DRY_RUN watchlist alert:\n%s", msg)
        return 0
    ok = send_telegram(msg, token=cfg.telegram_bot_token,
                       chat_id=cfg.telegram_chat_id, timeout=cfg.http_timeout_sec)
    log.info("watchlist alert sent: %s (%d การเปลี่ยน)", ok, len(changes))
    return 0 if ok else 1


# ─── News alerts (ข่าวด่วนหุ้นใน watchlist — เด้งทุก ~10 นาที, ออกแบบให้ $0) ──
_NEWS_SEEN_FILE = "news_seen.json"
_NEWS_SEEN_KEEP_H = 72   # เก็บ news-ID ที่เคยเตือน 3 วัน (กันซ้ำ) แล้ว prune ทิ้ง


def _newsworthy(headline: str) -> bool:
    """กรอง fluff เบา ๆ (เป้าหมาย 'ทันข่าว' → ตัดเฉพาะขยะชัด ๆ ที่ไม่ใช่ข่าวบริษัท)"""
    h = headline.strip()
    if len(h) < 15:
        return False
    low = h.lower()
    junk = ("here's what", "things to know", "stocks to watch", "stocks moving",
            "premarket movers", "what to watch", "week ahead", "market clubhouse",
            "movers & shakers", "biggest movers")
    return not any(j in low for j in junk)


def _zone_check_due() -> bool:
    """zone-check (yfinance หนัก) รันเฉพาะ ~04:30 และ ~16:40 ไทย (คงตารางเดิม)
    job ถูกเด้งทุก 10 นาที → เปิด 2 หน้าต่างนี้พอ กันรัน yfinance ถี่เกินเหตุ
    (zone-check idempotent ต่อ alert อยู่แล้ว — เตือนเฉพาะ 'โซนเปลี่ยน' → รันซ้ำไม่สแปม)"""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        now = _dt.now(ZoneInfo("Asia/Bangkok"))
    except Exception:  # noqa: BLE001
        return False
    if now.hour == 4 and 28 <= now.minute <= 38:
        return True
    if now.hour == 16 and 38 <= now.minute <= 48:
        return True
    return False


def run_news_alerts(cfg: Config) -> int:
    """เตือนข่าวด่วนของหุ้น US ที่ 'ถืออยู่' (watchlist) — เด้งทุก ~10 นาที
    เตือนเฉพาะข่าว ≤ LOOKBACK นาทีล่าสุด AND ยังไม่เคยเตือน (dedup ด้วย news-ID ใน GCS)
    → รอบแรกไม่ดั๊มป์ข่าวทั้งวัน, overlap window = ไม่มีรู, ไม่เตือนซ้ำ"""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    import concurrent.futures as cf
    try:
        from watchlist import store
        from data import fundamentals as fnd
    except Exception as e:  # noqa: BLE001
        log.warning("news module ไม่พร้อม: %s", e)
        return 0
    if not fnd.enabled():
        log.info("news alerts: Finnhub ไม่พร้อม (ไม่มี FINNHUB_API_KEY)")
        return 0
    try:
        positions = store.list_positions()
    except Exception as e:  # noqa: BLE001
        log.warning("โหลด watchlist (news) ไม่สำเร็จ: %s", e)
        return 0
    # Finnhub company-news ครอบคลุมหุ้น US เท่านั้น
    syms = sorted({(p.get("symbol") or "").upper() for p in positions
                   if p.get("market") == "us" and p.get("symbol")})
    if not syms:
        log.info("news alerts: ไม่มีหุ้น US ใน watchlist — ข้าม")
        return 0

    try:
        lookback_min = int(os.getenv("NEWS_LOOKBACK_MIN", "").strip() or "25")
    except ValueError:
        lookback_min = 25  # > รอบ scheduler (10) เพื่อ overlap กันข่าวหลุด
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())
    since = now_epoch - lookback_min * 60
    th = ZoneInfo("Asia/Bangkok")

    seen: dict = store.load_json(_NEWS_SEEN_FILE, {}) or {}

    # ดึงข่าวขนาน (cap 5 workers → burst < 60/นาที เสมอ ไม่ชน Finnhub limit)
    fetched: dict[str, list] = {}
    with cf.ThreadPoolExecutor(max_workers=min(5, len(syms))) as ex:
        futs = {ex.submit(fnd.news_items, s, since): s for s in syms}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                fetched[s] = fut.result() or []
            except Exception as e:  # noqa: BLE001
                log.warning("news fetch %s ล้มเหลว: %s", s, e)
                fetched[s] = []

    # เก็บข่าวใหม่ก่อน (ผ่าน dedup + fluff filter) แล้วค่อยแปลทีเดียว (batch กัน quota)
    new_items: list[dict] = []
    seen_headlines: set = set()
    for s in syms:
        for it in sorted(fetched.get(s, []), key=lambda x: x["datetime"]):
            nid = it["id"]
            if nid in seen:
                continue
            seen[nid] = now_epoch  # mark เห็นแล้วเสมอ (กันเตือนซ้ำรอบหน้า)
            hl = it["headline"]
            hkey = hl.lower()[:80]
            if hkey in seen_headlines or not _newsworthy(hl):
                continue
            seen_headlines.add(hkey)
            new_items.append({"s": s, "it": it})

    # วิเคราะห์ข่าวด้วย Gemini (กรองไม่เกี่ยว + สรุปไทย + ทิศจากเนื้อจริง + รวมข่าวซ้ำ)
    # ถ้า Gemini ใช้ไม่ได้ → fallback แปลพาดหัวธรรมดา (Google Translate) + ทิศจาก keyword
    ai_map: dict = {}
    try:
        from data import ai as _ai
        if _ai.enabled():
            ai_map = _ai.analyze_news([
                {"id": x["it"]["id"], "symbol": x["s"],
                 "headline": x["it"]["headline"], "summary": x["it"].get("summary", "")}
                for x in new_items
            ])
    except Exception as e:  # noqa: BLE001
        log.warning("วิเคราะห์ข่าว AI ไม่สำเร็จ: %s", e)
    use_ai = bool(ai_map)

    th_map: dict = {}
    if not use_ai:  # ไม่มี AI → แปลพาดหัวธรรมดา
        try:
            from data import translate as tr
            th_map = tr.to_thai([{"id": x["it"]["id"], "headline": x["it"]["headline"]} for x in new_items])
        except Exception as e:  # noqa: BLE001
            log.warning("แปลข่าวไม่สำเร็จ: %s", e)

    _ARROW = {"up": "📈", "down": "📉", "flat": "📰"}
    alerts: list[str] = []
    seen_clusters: set = set()
    for x in new_items:
        s, it = x["s"], x["it"]
        nid, hl_en = it["id"], it["headline"]
        if use_ai:
            a = ai_map.get(nid)
            if a:
                if not a.get("relevant", True):
                    continue  # ข่าวไม่กระทบหุ้นตัวนี้จริง → ข้าม
                cl = a.get("cluster", -1)
                if cl != -1 and cl in seen_clusters:
                    continue  # ข่าวเรื่องเดียวกันโชว์แล้ว → รวมเป็นอันเดียว
                if cl != -1:
                    seen_clusters.add(cl)
                hl_show = a.get("th") or hl_en
                arrow = _ARROW.get(a.get("dir", "flat"), "📰")
            else:  # ข่าวนี้ AI ไม่คืนผล → fallback
                hl_show, arrow = hl_en, fnd.news_direction(hl_en)
        else:
            hl_show = th_map.get(nid) or hl_en
            arrow = fnd.news_direction(hl_en)
        hhmm = datetime.fromtimestamp(it["datetime"], tz=timezone.utc).astimezone(th).strftime("%H:%M")
        block = f"{arrow} {s}\n{hl_show}"
        meta = [it["source"]] if it.get("source") else []
        meta.append(f"🕒 {hhmm}")
        block += "\n" + " · ".join(meta)
        if it.get("url"):
            block += f"\n{it['url']}"
        alerts.append(block)

    # prune seen เก่ากว่า KEEP ชม. แล้วเซฟกลับ
    cutoff = now_epoch - _NEWS_SEEN_KEEP_H * 3600
    seen = {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v >= cutoff}
    try:
        store.save_json(_NEWS_SEEN_FILE, seen)
    except Exception as e:  # noqa: BLE001
        log.warning("เซฟ news_seen ไม่สำเร็จ: %s", e)

    if not alerts:
        log.info("news alerts: ไม่มีข่าวใหม่ (เช็ก %d ตัว, lookback %d นาที)", len(syms), lookback_min)
        return 0

    msg = "📰 ข่าวด่วน Watchlist\n\n" + "\n\n".join(alerts)
    if cfg.dry_run:
        log.info("DRY_RUN news alert:\n%s", msg)
        return 0
    ok = send_telegram(msg, token=cfg.telegram_bot_token,
                       chat_id=cfg.telegram_chat_id, timeout=cfg.http_timeout_sec)
    log.info("news alert sent: %s (%d ข่าว)", ok, len(alerts))
    return 0 if ok else 1


# ─── Daily briefing (บรีฟพอร์ตเช้า by Gemini — สังเคราะห์ข้อมูลที่บอทมี) ──
_BRIEFING_STATE = "briefing_state.json"


def _briefing_due() -> bool:
    """บรีฟวันละครั้ง ~08:00 ไทย — กันส่งซ้ำด้วย state ใน GCS (เทียบวันที่)"""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        now = _dt.now(ZoneInfo("Asia/Bangkok"))
    except Exception:  # noqa: BLE001
        return False
    if not (now.hour == 8 and now.minute <= 12):
        return False
    today = now.strftime("%Y-%m-%d")
    try:
        from watchlist import store
        if (store.load_json(_BRIEFING_STATE, {}) or {}).get("last_date") == today:
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def _mark_briefing_done() -> None:
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from watchlist import store
        store.save_json(_BRIEFING_STATE,
                        {"last_date": _dt.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d")})
    except Exception as e:  # noqa: BLE001
        log.warning("mark briefing failed: %s", e)


def run_daily_briefing(cfg: Config) -> int:
    """บรีฟพอร์ตเช้า: รวมโซน+Stage+P/L+พื้นฐาน+มาโคร ของหุ้นที่ถือ → Gemini เรียบเรียงเป็นไทย
    สังเคราะห์ข้อมูลจริงเท่านั้น (ไม่ทำนาย) · ส่งเฉพาะเมื่อมี position + Gemini พร้อม"""
    import json as _json
    from data import ai as _ai
    if not _ai.enabled():
        log.info("briefing: Gemini ไม่พร้อม — ข้าม")
        return 0
    try:
        from watchlist import store
        from watchlist.tracker import full_status
        from data import fundamentals as fnd
    except Exception as e:  # noqa: BLE001
        log.warning("briefing module ไม่พร้อม: %s", e)
        return 0
    positions = store.list_positions()
    if not positions:
        log.info("briefing: พอร์ตว่าง — ข้าม")
        return 0

    rows: list[dict] = []
    for pos in positions:
        try:
            st = full_status(pos, crypto_exchange=cfg.crypto_exchange)
        except Exception as e:  # noqa: BLE001
            log.warning("briefing status %s ล้มเหลว: %s", pos.get("symbol"), e)
            continue
        stage = st.get("stage")
        pnl = st.get("pnl_pct")
        row = {
            "symbol": pos.get("display") or pos.get("symbol"),
            "side": pos.get("side"),
            "zone": st.get("current_zone"),
            "stage": stage.get("label") if stage else None,
            "pnl_pct": round(pnl, 1) if pnl is not None else None,
            "exit_alert": bool(st.get("exit_alert")),
        }
        if pos.get("market") == "us":
            try:
                f = fnd.fundamentals(row["symbol"])  # cached 12h
                a = f.get("analyst")
                if a:
                    row["analyst"] = a
                if f.get("last_surprise") is not None:
                    row["last_earnings_surprise_pct"] = f.get("last_surprise")
            except Exception:  # noqa: BLE001
                pass
            try:  # E) ปฏิทินงบ: นัดใกล้สุด (preview ถ้ายังไม่ถึง / recap ถ้าเพิ่งออก)
                ew = fnd.earnings_window(row["symbol"], back=2, ahead=7)
                if ew:
                    row["earnings"] = ew[0]
            except Exception:  # noqa: BLE001
                pass
        rows.append(row)

    macro = ""
    try:
        macro = fnd.macro_warning(7) or ""
    except Exception:  # noqa: BLE001
        pass

    prompt = (
        "คุณเป็นผู้จัดการกองทุนส่วนตัวที่สรุปให้นักลงทุนรายย่อยฟังตอนเช้า "
        "เขียน 'บรีฟพอร์ตเช้านี้' เป็นภาษาไทย กระชับ เป็นกันเอง อ่านลื่น "
        "ใช้ข้อความธรรมดา + อิโมจิ (ห้ามใช้ ** หรือ markdown ใด ๆ) ไม่ต้องทักทายยาว เข้าเรื่องเลย "
        "จากข้อมูลพอร์ตจริงด้านล่างเท่านั้น (ห้ามแต่งตัวเลข/ข้อมูลที่ไม่มี):\n"
        f"พอร์ตที่ถืออยู่: {_json.dumps(rows, ensure_ascii=False)}\n"
        f"ปฏิทินมาโคร US: {macro or 'ไม่มีเหตุการณ์เด่น'}\n\n"
        "โครงสร้างคำตอบ:\n"
        "1) ภาพรวมพอร์ตสั้น ๆ (กำไร/ขาดทุนรวมคร่าว ๆ จาก pnl_pct)\n"
        "2) ⚠️ ตัวที่ต้องจับตา/เสี่ยง — เน้นที่ stage เป็น Stage 4, exit_alert=true, หรือขาดทุนเยอะ\n"
        "3) 📅 เหตุการณ์สำคัญสัปดาห์นี้ (มาโคร + งบ) — ตัวที่มี field 'earnings' = ใกล้/เพิ่งประกาศงบ "
        "ให้เตือนวันประกาศ (date) และถ้ามี epsActual ให้บอก beat/miss เทียบ epsEstimate\n"
        "4) 👀 สิ่งที่ควร 'เฝ้าดู' วันนี้ (เชิงเฝ้าระวัง ไม่ใช่สั่งซื้อ/ขาย)\n"
        "ปิดท้ายบรรทัดเดียว: ℹ️ ข้อมูลประกอบการตัดสินใจ ไม่ใช่คำแนะนำลงทุน"
    )
    text = _ai.gemini(prompt, temperature=0.4)
    if not text or not str(text).strip():
        log.info("briefing: Gemini ไม่คืนผล — ข้าม")
        return 0
    msg = "☀️ บรีฟพอร์ตเช้านี้ (สรุปโดย AI)\n\n" + str(text).strip()
    if cfg.dry_run:
        log.info("DRY_RUN briefing:\n%s", msg)
        return 0
    ok = send_telegram(msg, token=cfg.telegram_bot_token,
                       chat_id=cfg.telegram_chat_id, timeout=cfg.http_timeout_sec)
    log.info("briefing sent: %s", ok)
    return 0 if ok else 1


# ─── Weekly review (รีวิวสัปดาห์ ศุกร์เย็น by Gemini) ────────────────────
_WEEKLY_STATE = "weekly_state.json"


def _weekly_due() -> bool:
    """รีวิวสัปดาห์ ศุกร์ ~17:00 ไทย — กันซ้ำด้วย ISO week (GCS)"""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        now = _dt.now(ZoneInfo("Asia/Bangkok"))
    except Exception:  # noqa: BLE001
        return False
    if now.weekday() != 4 or not (now.hour == 17 and now.minute <= 12):  # 4=ศุกร์
        return False
    wk = now.strftime("%G-%V")
    try:
        from watchlist import store
        if (store.load_json(_WEEKLY_STATE, {}) or {}).get("last_week") == wk:
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def _mark_weekly_done() -> None:
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from watchlist import store
        store.save_json(_WEEKLY_STATE,
                        {"last_week": _dt.now(ZoneInfo("Asia/Bangkok")).strftime("%G-%V")})
    except Exception as e:  # noqa: BLE001
        log.warning("mark weekly failed: %s", e)


def run_weekly_review(cfg: Config) -> int:
    """รีวิวสัปดาห์: สถิติเทรดสะสม (journal) + พอร์ตปัจจุบัน → Gemini เขียนรีวิว/บทเรียนเป็นไทย"""
    import json as _json
    from data import ai as _ai
    if not _ai.enabled():
        return 0
    try:
        from watchlist import store
        from watchlist import journal as wl_journal
        from watchlist.tracker import full_status
    except Exception as e:  # noqa: BLE001
        log.warning("weekly module ไม่พร้อม: %s", e)
        return 0
    stats: dict = {}
    try:
        stats = wl_journal.compute_stats() or {}
    except Exception:  # noqa: BLE001
        pass
    port: list[dict] = []
    for pos in store.list_positions():
        try:
            st = full_status(pos, crypto_exchange=cfg.crypto_exchange)
        except Exception:  # noqa: BLE001
            continue
        stage = st.get("stage")
        pnl = st.get("pnl_pct")
        port.append({"symbol": pos.get("display") or pos.get("symbol"), "side": pos.get("side"),
                     "zone": st.get("current_zone"), "stage": stage.get("label") if stage else None,
                     "pnl_pct": round(pnl, 1) if pnl is not None else None})
    if not stats and not port:
        log.info("weekly: ไม่มีข้อมูล — ข้าม")
        return 0
    prompt = (
        "คุณเป็นโค้ชเทรดดิ้งที่สรุปผลให้ลูกศิษย์ทุกสุดสัปดาห์ เขียน 'รีวิวประจำสัปดาห์' เป็นไทย "
        "กระชับ ให้กำลังใจแต่ตรงไปตรงมา ใช้ข้อความธรรมดา + อิโมจิ (ห้าม markdown) จากข้อมูลจริงเท่านั้น:\n"
        f"สถิติเทรดสะสม (ไม้ที่ปิดแล้ว): {_json.dumps(stats, ensure_ascii=False) if stats else 'ยังไม่มีไม้ที่ปิด'}\n"
        f"พอร์ตที่ถืออยู่ตอนนี้: {_json.dumps(port, ensure_ascii=False) if port else 'ไม่มี'}\n\n"
        "โครงสร้าง: 1) ภาพรวมพอร์ต+ผลงานสะสม 2) 👍 ทำได้ดี 3) 🔧 จุดที่ควรปรับ (วินัย/ตัวที่ Stage 4 หรือขาดทุน) "
        "4) 🎯 โฟกัสสัปดาห์หน้า. ปิดท้ายบรรทัดเดียว: ℹ️ ไม่ใช่คำแนะนำลงทุน"
    )
    text = _ai.gemini(prompt, temperature=0.5)
    if not text or not str(text).strip():
        return 0
    msg = "📆 รีวิวประจำสัปดาห์ (สรุปโดย AI)\n\n" + str(text).strip()
    if cfg.dry_run:
        log.info("DRY_RUN weekly:\n%s", msg)
        return 0
    ok = send_telegram(msg, token=cfg.telegram_bot_token,
                       chat_id=cfg.telegram_chat_id, timeout=cfg.http_timeout_sec)
    log.info("weekly review sent: %s", ok)
    return 0 if ok else 1


# ─── entry point ──────────────────────────────────────────────────────

def main() -> int:
    try:
        cfg = load_config()
    except Exception as e:
        log.error("config error: %s", e)
        return 2

    # โหมดวินิจฉัยสภาพคล่อง option: DIAG_LIQUIDITY="AMZN,GOOGL,..." → log ค่าจริงแล้วจบ
    diag = os.getenv("DIAG_LIQUIDITY", "").strip()
    if diag:
        from data.market import option_liquidity
        from data.quote import last_price
        try:
            diag_dte = int(os.getenv("DIAG_DTE", "").strip() or "0") or None
        except ValueError:
            diag_dte = None
        for tk in [x.strip().upper() for x in diag.replace(";", ",").split(",") if x.strip()]:
            try:
                spot = last_price("us", tk)
                liq = option_liquidity(tk, spot, target_dte=diag_dte,
                                       min_oi=cfg.min_option_oi, max_spread_pct=cfg.max_option_spread)
                log.info("DIAG %s spot=%s dte=%s → status=%s near_oi=%s spread=%s exp=%s",
                         tk, spot, diag_dte, liq.get("status"), liq.get("oi"),
                         liq.get("spread_pct"), liq.get("expiry"))
            except Exception as e:  # noqa: BLE001
                log.exception("DIAG %s failed: %s", tk, e)
        return 0

    if os.getenv("WATCHLIST_ALERT_ONLY", "").strip().lower() in ("1", "true", "yes", "on"):
        # job นี้ถูกเด้งทุก ~10 นาที → ข่าวด่วนทุกรอบ (เบา) + เช็กโซนเฉพาะ ~04:30/16:40 (หนัก)
        log.info("=== Watchlist poller (ข่าวด่วนทุกรอบ + เช็กโซนตามเวลา) ===")
        rc = run_news_alerts(cfg)
        if _zone_check_due():
            log.info("ถึงหน้าต่างเช็กโซน watchlist")
            rc_zone = run_watchlist_alerts(cfg)
            rc = rc or rc_zone
        if _briefing_due():  # บรีฟพอร์ตเช้า ~08:00 (วันละครั้ง) — รวมปฏิทินงบ (E)
            log.info("ถึงเวลาบรีฟพอร์ตเช้า")
            run_daily_briefing(cfg)
            _mark_briefing_done()
        if _weekly_due():  # รีวิวสัปดาห์ ศุกร์ ~17:00 (F)
            log.info("ถึงเวลารีวิวประจำสัปดาห์")
            run_weekly_review(cfg)
            _mark_weekly_done()
        return rc

    runners, is_full = _select_runners()
    log.info("CDC Scanner starting | exchange=%s | filters=%s | dry_run=%s | groups=%s",
             cfg.crypto_exchange, cfg.enable_filters, cfg.dry_run,
             os.getenv("SCAN_GROUPS", "all") or "all")

    results: List[GroupResult] = []
    for run in runners:
        try:
            results.append(run(cfg))
        except Exception as e:  # noqa: BLE001 — กันกลุ่มเดียวพังทั้งระบบ
            log.exception("group runner %s ล่ม: %s", run.__name__, e)

    messages = build_messages(results, cfg)

    # รายงาน watchlist เฉพาะตอนสแกนครบทุกกลุ่ม (สแกนรายหมวด = โฟกัสสัญญาณหมวดนั้น)
    if is_full:
        watchlist_msg = build_watchlist_report(cfg)
        if watchlist_msg:
            messages.append(watchlist_msg)
        # สะพาน Part 1 → Part 2 (MT5): ปล่อยสัญญาณลง GCS (เสริม ไม่กระทบสแกน)
        try:
            from signals_export import export_signals
            export_signals(results, cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("export signals (Part 2 bridge) failed: %s", e)
        # D1: บันทึก signal log + ประเมินผล forward-test (วัดความแม่นด้วยหลักฐาน) — best-effort
        try:
            import signals_log
            signals_log.log_signals(results, cfg)
            signals_log.evaluate_outcomes(cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("signals_log failed: %s", e)

    if cfg.dry_run:
        log.info("DRY_RUN: %d message(s) to send (ไม่ส่งจริง)", len(messages))
        for i, m in enumerate(messages, 1):
            log.info("--- message %d ---\n%s", i, m)
        return 0

    sent_ok = 0
    for m in messages:
        if send_telegram(m, token=cfg.telegram_bot_token,
                         chat_id=cfg.telegram_chat_id,
                         timeout=cfg.http_timeout_sec):
            sent_ok += 1

    log.info("done — sent %d/%d messages", sent_ok, len(messages))
    return 0 if sent_ok == len(messages) else 1


if __name__ == "__main__":
    sys.exit(main())
