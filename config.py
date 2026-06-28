"""
config.py — รวมค่า config ทุกอย่างไว้ที่เดียว
อ่านจาก environment variables (Cloud Run --set-secrets / Secret Manager / .env)
"""
from __future__ import annotations
import os
from dataclasses import dataclass


def _get_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # ── Telegram ────────────────────────────────────────────────────
    telegram_bot_token: str
    telegram_chat_id: str

    # ── CDC parameters ──────────────────────────────────────────────
    ema_fast: int = 12
    ema_slow: int = 26
    ema_trend: int = 200          # ใช้ใน confluence filter
    adx_period: int = 14
    rsi_period: int = 14
    vol_sma_period: int = 20

    # ── Confluence filter ───────────────────────────────────────────
    enable_filters: bool = True   # คำนวณ score เสมอ (ไม่ตัดทิ้ง)
    min_score_to_alert: int = 0   # 0 = รายงานทุกสัญญาณ (ค่าแนะนำ)
    alert_high_quality_only: bool = True  # โชว์เฉพาะ HIGH-QUALITY (≥3 ดาว + ผ่านเทรนด์) = ตัวน่าเข้าจริง
    show_filter_breakdown: bool = True  # โชว์ว่าผ่าน filter ตัวไหน

    # ── Multi-timeframe (เทรนด์รายสัปดาห์) ───────────────────────────
    enable_mtf: bool = True       # คำนวณว่าสัญญาณตรงเทรนด์รายสัปดาห์ไหม
    require_mtf: bool = False      # True = ส่งเฉพาะสัญญาณที่ตรง weekly (default ไม่ตัด)

    # ── Hard filters (ตัดทิ้งเลย ไม่แสดงใน scan) ─────────────────────
    min_adx_to_alert: float = 25.0       # ADX ≤ นี้ = ตัด (sideway)
    require_volume_above_sma: bool = True # volume ≤ SMA20 = ตัด (ไม่มีแรง)
    max_rsi_buy: float = 69.0            # Buy: RSI ≥ นี้ = ตัด (overbought ไล่ของแพง)

    # ── Relative Strength (จัดอันดับเทียบทั้งกลุ่ม — แบบกองทุน momentum) ──
    enable_rs: bool = True               # คำนวณ RS rank (เทียบความแข็งภายในกลุ่ม)
    min_rs_buy: float = 50.0             # Buy: RS < นี้ = ตัด (อ่อนกว่าครึ่งตลาด) · 0 = ปิด
    max_rs_sell: float = 50.0            # Sell: RS > นี้ = ตัด (แข็งเกินไปไม่เหมาะ Put) · 100 = ปิด
    rs_hard_gate: bool = True            # True = ตัดทิ้งตาม min_rs_buy/max_rs_sell (เดิม) · False = ใช้ RS
                                         # แค่จัดอันดับ ไม่ตัด (RS percentile มี survivorship bias —
                                         # แนะนำตั้ง false หลังดู /calib ว่า low-RS แพ้จริงไหม) [C2]

    # ── Market regime (ดัชนีอ้างอิงเหนือ EMA200 ไหม — โชว์สถานะตลาด) ──
    enable_regime: bool = True           # โชว์สถานะตลาดรวมในหัวข้อความ scan

    # ── Fundamentals (Finnhub/FMP — นักวิเคราะห์ + งบ + มาโคร) ──
    enable_fundamentals: bool = True     # โชว์ flag พื้นฐาน/นักวิเคราะห์ใน scan กลุ่ม + มาโครบนหัว

    # ── Reversal watch (หุ้นใกล้กลับตัว 🔵/🟠 — ดักก่อนสัญญาณ, ข้าม hard filters) ──
    enable_reversal_watch: bool = True   # โชว์ section "ใกล้กลับตัว" ใน /scan
    reversal_max: int = 6                # จำกัดต่อทิศ (เรียงตาม RS) — block เต็มยาว กันท่วม
    reversal_fresh_only: bool = True     # True = เฉพาะ "เพิ่งเข้าโซนกลับตัว" (กันเตือนตัวเดิมซ้ำทุกวัน)

    # ── Data staleness guard (กันยิงสัญญาณบนราคาค้าง/feed ล่ม) ──
    max_stale_days_crypto: int = 3       # crypto: แท่งล่าสุดเก่ากว่านี้ (วัน) = ค้าง → ตัด
    max_stale_days_equity: int = 8       # หุ้น/ทอง: เผื่อหยุดยาว (Songkran/Thanksgiving cluster) + เสาร์อาทิตย์ + margin

    # ── Sideway filter (กันแนะนำตัวไร้เทรนด์ — สมดุล: ADX ต่ำ + MA150 แบน, เก็บ early-trend) ──
    filter_sideway: bool = True          # ตัด sideway ออกจากหมวดใกล้กลับตัว
    sideway_adx_max: float = 20.0        # ADX ต่ำกว่านี้ + MA150 แบน = sideway

    # ── Setup quality (14-มิติ price action → ปรับ "อันดับ" ไม่ตัดทิ้ง ไม่แตะนิยาม HQ) ──
    # รวม breakout/gap/แท่งยาว/โครงสร้าง HH-HL/แรงขาย-ซื้อ (distribution)/trendline + หักดอย/ขรุขระ
    enable_setup_quality: bool = True    # คำนวณ + พับ setup_score เข้าการจัดอันดับ (ดันตัว setup ดีขึ้นบน)
    show_setup_quality: bool = True      # โชว์บรรทัด 🧭 Setup สรุปปัจจัยใต้สัญญาณ

    # ── Option liquidity (กันแนะนำ option ที่ขายต่อไม่ออก — US เท่านั้น) ──
    enable_option_liquidity: bool = True  # เช็ก OI/spread ของสัญญาณที่โชว์ แล้วกรองคำแนะนำ option
    min_option_oi: int = 500              # OI รวมใกล้ ATM (call+put) ต่ำกว่านี้ = แย่ (ไม่แนะนำ)
    max_option_spread: float = 40.0       # spread % เกินนี้ = แย่ (ผ่อนเพราะ scan รันหลังตลาดปิด spread บาน)

    # ── Crypto exchange ─────────────────────────────────────────────
    # binance: Cloud Run สิงคโปร์ใช้ได้
    # bybit / okx: fallback (เผื่อ Binance ตอบ 451 — แม้ region เอเชียก็เคยโดน)
    crypto_exchange: str = "binance"
    crypto_top_n: int = 50
    crypto_min_volume_usdt: float = 1_000_000.0

    # ── Universe sizes / safety ─────────────────────────────────────
    min_bars_required: int = 60   # ถ้า ENABLE_EMA200_FILTER จะใช้ 220 แทน
    enable_ema200_filter: bool = True

    # ── Network ─────────────────────────────────────────────────────
    http_timeout_sec: int = 30
    retry_attempts: int = 3
    retry_base_delay_sec: float = 1.5

    # ── Misc ────────────────────────────────────────────────────────
    dry_run: bool = False         # ถ้า True จะไม่ส่ง Telegram แค่ log


def load_config() -> Config:
    """โหลด config จาก env. token / chat id ต้องมี (ยกเว้น dry_run=True)"""
    dry_run = _get_bool("DRY_RUN", False)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not dry_run and (not token or not chat_id):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ไม่ถูกตั้งค่า "
            "(ใช้ Secret Manager หรือ env var)"
        )

    return Config(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        enable_filters=_get_bool("ENABLE_FILTERS", True),
        min_score_to_alert=_get_int("MIN_SCORE_TO_ALERT", 0),
        alert_high_quality_only=_get_bool("ALERT_HQ_ONLY", True),
        show_filter_breakdown=_get_bool("SHOW_FILTER_BREAKDOWN", True),
        enable_mtf=_get_bool("ENABLE_MTF", True),
        require_mtf=_get_bool("REQUIRE_MTF", False),
        min_adx_to_alert=_get_float("MIN_ADX", 25.0),
        require_volume_above_sma=_get_bool("REQUIRE_VOLUME", True),
        max_rsi_buy=_get_float("MAX_RSI_BUY", 69.0),
        enable_rs=_get_bool("ENABLE_RS", True),
        min_rs_buy=_get_float("MIN_RS_BUY", 50.0),
        max_rs_sell=_get_float("MAX_RS_SELL", 50.0),
        rs_hard_gate=_get_bool("RS_HARD_GATE", True),
        enable_regime=_get_bool("ENABLE_REGIME", True),
        enable_fundamentals=_get_bool("ENABLE_FUNDAMENTALS", True),
        enable_reversal_watch=_get_bool("ENABLE_REVERSAL", True),
        reversal_max=_get_int("REVERSAL_MAX", 6),
        reversal_fresh_only=_get_bool("REVERSAL_FRESH_ONLY", True),
        max_stale_days_crypto=_get_int("MAX_STALE_DAYS_CRYPTO", 3),
        max_stale_days_equity=_get_int("MAX_STALE_DAYS_EQUITY", 8),
        filter_sideway=_get_bool("FILTER_SIDEWAY", True),
        sideway_adx_max=_get_float("SIDEWAY_ADX_MAX", 20.0),
        enable_setup_quality=_get_bool("ENABLE_SETUP_QUALITY", True),
        show_setup_quality=_get_bool("SHOW_SETUP_QUALITY", True),
        enable_option_liquidity=_get_bool("ENABLE_OPTION_LIQUIDITY", True),
        min_option_oi=_get_int("MIN_OPTION_OI", 500),
        max_option_spread=_get_float("MAX_OPTION_SPREAD", 40.0),
        crypto_exchange=os.getenv("CRYPTO_EXCHANGE", "binance").strip().lower(),
        crypto_top_n=_get_int("CRYPTO_TOP_N", 50),
        enable_ema200_filter=_get_bool("ENABLE_EMA200_FILTER", True),
        min_bars_required=_get_int("MIN_BARS_REQUIRED", 60),
        dry_run=dry_run,
    )
