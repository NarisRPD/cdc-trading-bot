"""
part2_mt5/ticket.py — ประกอบ "ใบสั่งเทรด" จาก bias (SuperTrend/HalfTrend/UT Bot บนข้อมูล MT5) + วิเคราะห์ Part 2

ขั้นตอน: ดึง OHLC entry-TF → แท่งเทียน/วอลุ่ม/โครงสร้าง → SL จากโครงสร้าง + TP R:R
→ lot จากสเปกโบรกจริง → risk gate → Gemini ปิดช่องโหว่ → ข้อความใบสั่ง (คนกดเอง)

bias มาจาก _scan_supertrend / _scan_halftrend / _scan_utbot (ราคา Exness จริง)
"""
from __future__ import annotations
import logging
from typing import Optional

import candles
import market_hours
import patterns
import risk
import gemini_gate
import learn
import shadow
import scalp as _scalp_module   # ใช้ vpoc() — import ที่ module level เพื่อกัน shadowing กับ param scalp
import scalp_filters             # Pro scalping filters (Kill Zone/Liquidity/Momentum/VWAP)

log = logging.getLogger("part2.ticket")

_DIR_TH = {"buy": "🟢 Buy (ซื้อ/Long)", "sell": "🔴 Sell (ขาย/Short)"}
_DECISION_TH = {"enter": "✅ เข้าได้", "small": "⚠️ เข้าไม้เล็ก", "skip": "⛔ ข้าม", "manual": "🔎 ตรวจเอง"}


def _atr(df, n: int = 14) -> float:
    import numpy as np
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    pc = np.roll(c, 1)
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return float(np.mean(tr[-n:]))


# กลยุทธ์ "สวนเทรนด์โดยตั้งใจ" (mean-reversion) — ไม่บังคับ MTF align (ไม่งั้นตัดทิ้งหมด)
_MEAN_REVERSION_SRC = {"vwap", "rsi_div", "range_mr"}

# กลยุทธ์ที่มี "trend gate ของตัวเอง" แข็งแรงกว่า MTF — ยกเว้น MTF filter เสมอ
# (rsi2: เข้าเฉพาะเหนือ/ใต้ EMA200 อยู่แล้ว · จังหวะเข้าคือ "ย่อ" ซึ่ง M30 มักสวนชั่วคราว
#  ถ้าให้ MTF เช็คจะบล็อกหมด — ขัดธรรมชาติ buy-the-dip ของกลยุทธ์)
_OWN_TREND_SRC = {"rsi2"}

# กลยุทธ์ที่ยกเว้น regime filter: mean-rev (range คือ edge ของมัน) + rvol_brk
# (RVOL spurt = หลักฐานว่า regime เพิ่งเปลี่ยน — ADX H1 เป็น lagging ตามไม่ทัน
#  ถ้าไม่ยกเว้น breakout จาก consolidation จะโดนบล็อกว่า "range" ทุกครั้ง)
_REGIME_EXEMPT_SRC = _MEAN_REVERSION_SRC | {"rvol_brk"}

# กลยุทธ์ scalp/สั้น ที่ Pro Scalping Filters เหมาะ (Kill Zone/Liquidity/Momentum/VWAP)
# ไม่รวม trend H1 (supertrend/halftrend/pa) เพราะ Kill Zone จะบล็อกนอก session ผิดเจตนา
# ไม่รวม orb_pro/fx_orb — กลยุทธ์เล่น "ช่วงเปิด session" โดยเฉพาะ ซึ่งตอนนั้น
#   ราคาติด VWAP (เพิ่งรีเซ็ตวัน) + ADX ยัง lag → Pro filters บล็อก 100% ทั้งที่มี
#   เกราะของตัวเองครบ (ความกว้างกรอบ vs ATR · กัน late entry · จำกัดหน้าต่างเวลา)
_SCALP_FILTER_SRC = {"vwap", "rsi_div", "bb_squeeze", "ema_m5",
                     "scalp", "utbot", "hybrid"}

# กลยุทธ์ที่ Momentum confirm "ขัดธรรมชาติ" — ปิดเฉพาะ sub-filter นั้นให้ (ตัวอื่นยังตรวจ)
#   mean-reversion (rsi_div/vwap/range_mr): เข้าสวนโมเมนตัมล่าสุดโดยนิยาม
#   bb_squeeze: squeeze เกิดจาก ADX ต่ำโดยนิยาม → เกณฑ์ ADX≥20 = ไม่มีวันผ่าน
# (หลักฐาน 10 มิ.ย.: "Momentum อ่อน" 156 ครั้ง/400 บรรทัด — กลยุทธ์พวกนี้ไม่เคยผ่านเลย)
_MOMENTUM_EXEMPT = _MEAN_REVERSION_SRC | {"bb_squeeze"}


def _higher_tf_trend(exsym: str, mtf_tf: str, mt5) -> str:
    """ทิศเทรนด์ TF ใหญ่: 'up' / 'down' / 'neutral' จาก EMA50 vs EMA200 + ตำแหน่งราคา
    ใช้บล็อกไม้สวนเทรนด์ (MTF alignment filter) · ข้อมูลไม่พอ → 'neutral' (ไม่บล็อก)"""
    df = mt5.rates(exsym, mtf_tf, 220)
    if df is None or len(df) < 200:
        return "neutral"
    c = df["close"].astype(float)
    e50 = float(c.ewm(span=50, adjust=False).mean().iloc[-1])
    e200 = float(c.ewm(span=200, adjust=False).mean().iloc[-1])
    price = float(c.iloc[-1])
    if e50 > e200 and price > e200:
        return "up"
    if e50 < e200 and price < e200:
        return "down"
    return "neutral"       # EMA พันกัน/ราคาอยู่กลาง = ไม่มีเทรนด์ชัด → ไม่บล็อก


def build_ticket(exsym: str, bias: dict, account: dict, cfg: dict, mt5,
                 part1_hint: Optional[dict] = None, scalp: Optional[dict] = None) -> Optional[dict]:
    # ประเภทสินทรัพย์ — ใช้ตลอด function (spread / RSI threshold)
    _sym_cat = market_hours.category(exsym)

    # ปิดเทรด FX ทั้งหมดถ้า TRADE_FX=false (FX ขยับน้อย — ผู้ใช้เลี่ยง) · จุดเดียวคุมทุกกลยุทธ์
    # ยกเว้นกลยุทธ์ใน TRADE_FX_ALLOW (เช่น ema_m5 — สถิติจริง 11 มิ.ย. win 67% ดีสุดของบอท)
    if _sym_cat == "fx" and cfg.get("TRADE_FX", "true").lower() not in ("1", "true", "yes", "on"):
        _fx_allow = {s.strip().lower() for s in cfg.get("TRADE_FX_ALLOW", "").split(",") if s.strip()}
        if (bias.get("source") or "").lower() not in _fx_allow:
            log.debug("ข้าม %s — ปิดเทรด FX (TRADE_FX=false · source=%s ไม่อยู่ใน allow)",
                      exsym, bias.get("source"))
            return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                    "reason": "ปิดเทรด FX (TRADE_FX=false)"}

    # FX Session Mode — เทรด FX เฉพาะ "นอกเวลาตลาด US" (Asian/London)
    # ช่วง US เปิด volatility ข้ามตลาดสูง FX โดนลาก → หยุดเปิดไม้ใหม่ตั้งแต่
    # FX_FLATTEN_BEFORE_US_MIN นาทีก่อน US เปิด (ไม้ที่ถืออยู่ manage.py ปิดให้หมด)
    # หลัง US ปิด (03:00 ไทย) กลับมาเทรดได้อัตโนมัติ
    if (_sym_cat == "fx"
            and cfg.get("FX_SESSION_MODE", "false").lower() in ("1", "true", "yes", "on")):
        _fx_buf = int(cfg.get("FX_FLATTEN_BEFORE_US_MIN", "30") or "30")
        if market_hours.in_us_session(_fx_buf):
            log.debug("ข้าม %s — FX หยุดช่วงตลาด US (รวม buffer %d นาที)", exsym, _fx_buf)
            return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                    "reason": "FX หยุดช่วงตลาด US — กลับมาเทรดหลัง 03:00 ไทย"}

    # ปิดดัชนีที่ไม่ใช่ US ถ้า TRADE_INDEX=false — ยกเว้น allowlist (ดีฟอลต์ ญี่ปุ่น JP225 + ฮ่องกง HK50)
    # ดัชนี US (category us_index) ไม่โดน guard นี้ — เทรดได้ปกติ
    if _sym_cat == "index" and cfg.get("TRADE_INDEX", "true").lower() not in ("1", "true", "yes", "on"):
        _idx_allow = [a.strip().upper() for a in cfg.get("INDEX_ALLOW", "JP225,HK50").split(",") if a.strip()]
        if not any(exsym.upper().startswith(a) for a in _idx_allow):
            log.debug("ข้าม %s — ปิดเทรดดัชนี (TRADE_INDEX=false · ไม่อยู่ใน allowlist)", exsym)
            return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                    "reason": "ปิดเทรดดัชนี (TRADE_INDEX=false)"}

    # ตรวจตลาดเปิดอยู่ไหม — ข้ามเงียบถ้าปิด (ลด Gemini call + log noise)
    # หุ้น US นอกเวลา: spread กว้าง 5–10× ปกติ → ไม่มีประโยชน์สแกน
    if not market_hours.is_open(exsym):
        log.debug("ข้าม %s — ตลาดปิด (%s)", exsym, _sym_cat)
        return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                "reason": "ตลาดปิด"}

    # ตรวจช่วง opening/closing range ของตลาด US — volatility พุ่ง สัญญาณ false เยอะ
    # Opening: 30 นาทีแรกหลัง 20:30 ไทย · Closing: 15 นาทีก่อน 03:00 ไทย
    # ยกเว้น orb_pro — กลยุทธ์เดียวที่ออกแบบมาเล่นช่วง open โดยเฉพาะ
    # (opening range + SL สั้นใต้กรอบ + trade window จำกัด = มีเกราะของตัวเองครบ)
    _open_skip  = int(cfg.get("US_OPEN_SKIP_MIN",  "30") or "30")
    _close_skip = int(cfg.get("US_CLOSE_SKIP_MIN", "15") or "15")
    if (bias.get("source") != "orb_pro"
            and market_hours.in_volatile_window(exsym, _open_skip, _close_skip)):
        log.debug("ข้าม %s — ช่วง opening/closing range US (skip %d/%d นาที)", exsym, _open_skip, _close_skip)
        return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                "reason": f"ช่วงเปิด/ปิดตลาด US (volatile window {_open_skip}/{_close_skip}min)"}

    # ห้ามเปิดไม้ใหม่เมื่อตลาดใกล้ปิด — runway สั้นเกิน trailing ไม่ทันทำงาน
    # แล้วโดน CLOSE_BEFORE_MARKET_CLOSE (buffer 20 นาที) บังคับปิดทันที → ต้องตั้งค่านี้ > 20
    # crypto คืน False เสมอ (24 ชม.) · FX/commodity โดนเฉพาะก่อนปิดสุดสัปดาห์
    _no_entry_min = int(cfg.get("NO_ENTRY_BEFORE_CLOSE_MIN", "30") or "30")
    if _no_entry_min > 0 and market_hours.closing_soon(exsym, _no_entry_min):
        log.debug("ข้าม %s — เหลือ <%d นาทีก่อนตลาดปิด ไม่เปิดไม้ใหม่", exsym, _no_entry_min)
        return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                "reason": f"ใกล้ตลาดปิด (<{_no_entry_min} นาที) — ไม่เปิดไม้ใหม่"}

    # Auto-disable เทคนิคที่แพ้: กลยุทธ์/symbol ที่ win-rate ต่ำกว่าเกณฑ์ในข้อมูลจริง → ข้ามเงียบ
    # (ใช้ผลเทรดจริงนำ — block เฉพาะตัวที่มีหลักฐานแพ้ชัด · ดู learn.should_skip)
    # ยกเว้นกลยุทธ์ช่วงทดลองงาน (shadow) — paper trade เสียเงินไม่ได้ ต้องปล่อยให้สะสมสถิติแก้ตัว
    # ไม่งั้นสถิติแช่แข็ง (โดนพัก → ไม่มีไม้ใหม่ → win rate ค้างต่ำตลอดกาล = ขังถาวร)
    if (bias.get("source") or "").lower() not in shadow.shadow_set(cfg):
        _skip, _skip_reason = learn.should_skip(bias.get("source", ""), exsym, cfg)
        if _skip:
            log.info("⛔ ข้าม %s — %s", exsym, _skip_reason)
            return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                    "reason": _skip_reason}

    # เกราะพอร์ตขั้นต่ำสำหรับโลหะ: ทอง/เงินไม้ขั้นต่ำ (0.01 lot) ใหญ่เกินพอร์ตเล็ก
    # → ปลดล็อกให้เทรดเมื่อพอร์ตถึงเกณฑ์ (GOLD_MIN_BALANCE / SILVER_MIN_BALANCE) อัตโนมัติ
    _u = exsym.upper()
    _mmin = (float(cfg.get("GOLD_MIN_BALANCE", "0") or "0") if _u.startswith("XAU")
             else float(cfg.get("SILVER_MIN_BALANCE", "0") or "0") if _u.startswith("XAG") else 0.0)
    _bal = account.get("balance", 0) or 0
    if _mmin > 0 and _bal > 0 and _bal < _mmin:
        return {"skipped": True, "exsym": exsym, "direction": bias.get("direction", "buy"),
                "reason": f"พอร์ต ${_bal:.0f} ยังไม่ถึง ${_mmin:.0f} — ปลดล็อก"
                          f"{'ทอง' if _u.startswith('XAU') else 'เงิน'}ที่นั่น"}
    entry_tf = cfg.get("ENTRY_TF", "H1")
    df = mt5.rates(exsym, entry_tf, 200)
    if df is None or len(df) < 60:
        return None
    direction = bias.get("direction", "buy")
    px = mt5.price(exsym)
    if not px:
        return None
    spot = px["ask"] if direction == "buy" else px["bid"]
    atr = _atr(df, 14)

    # ── เกราะ RSI สุดขั้ว ────────────────────────────────────────────────────
    # ใช้ TF ที่ตรงกับเทคนิค ไม่ใช้ ENTRY_TF เสมอ
    # เหตุผล: UT Bot M15 ควรเช็ค RSI บน M15 ไม่ใช่ H1 (entry_tf อาจต่างกัน)
    _rsi_tf_map = {
        # H1 strategies — ตรวจ RSI บน H1
        "supertrend": cfg.get("ST_TF",    "H1"),
        "halftrend":  cfg.get("HT_TF",    "H1"),
        "pa":         cfg.get("PA_TF",    "H1"),
        # M15 strategies
        "utbot":      cfg.get("UTB_TF",   "M15"),
        "hybrid":     "M15",
        "scalp":      cfg.get("SCALP_TF", entry_tf),   # EMA+Stoch ใช้ SCALP_TF (fallback entry_tf)
        # M5 scalp suite — ตรวจ RSI บน M5 เหมือน TF ที่ scanner ใช้
        "ema_m5":     cfg.get("EMA_M5_TF", "M5"),
        "vwap":       "M5",
        "bb_squeeze": "M5",
        "rsi_div":    "M5",
        "orb_pro":    "M5",
        "fx_orb":     "M5",
        "range_mr":   cfg.get("RANGE_MR_TF", "M15"),
        "rvol_brk":   cfg.get("RVOL_TF", "M15"),
        "rsi2":       cfg.get("RSI2_TF", "H1"),
    }
    rsi_tf = _rsi_tf_map.get(bias.get("source", ""), entry_tf)   # fallback → entry_tf
    if rsi_tf.upper() != entry_tf.upper():
        _df_rsi_raw = mt5.rates(exsym, rsi_tf, 60)
        _df_rsi = _df_rsi_raw if (_df_rsi_raw is not None and len(_df_rsi_raw) >= 30) else df
    else:
        _df_rsi = df

    # RSI threshold แยก commodity — ทอง/น้ำมันวิ่ง overbought ได้นานในเทรนด์ (RSI 80+ ปกติมากสำหรับทอง bull)
    # ใช้ค่า fallback จาก RSI_OVERSOLD/RSI_OVERBOUGHT ถ้าไม่ได้ตั้ง _COMMODITY ไว้
    if _sym_cat == "commodity":
        rsi_ovs = float(cfg.get("RSI_OVERSOLD_COMMODITY",  cfg.get("RSI_OVERSOLD",   "20")))
        rsi_obt = float(cfg.get("RSI_OVERBOUGHT_COMMODITY", cfg.get("RSI_OVERBOUGHT", "82")))
    else:
        rsi_ovs = float(cfg.get("RSI_OVERSOLD",   "30"))   # block sell เมื่อ RSI < ค่านี้
        rsi_obt = float(cfg.get("RSI_OVERBOUGHT",  "70"))  # block buy เมื่อ RSI > ค่านี้

    import numpy as _np
    _c = _df_rsi["close"].astype(float)
    _d = _c.diff()
    _up = _d.clip(lower=0).rolling(14).mean()
    _dn = (-_d.clip(upper=0)).rolling(14).mean().replace(0, _np.nan)
    rsi_tf_val = float((100 - 100 / (1 + _up / _dn)).fillna(50).iloc[-1])
    if direction == "sell" and rsi_tf_val < rsi_ovs:
        log.info("ข้าม %s — RSI(%s) %.0f oversold (ไม่ช็อตก้นเหว)", exsym, rsi_tf, rsi_tf_val)
        return {"skipped": True, "exsym": exsym, "direction": direction,
                "reason": f"RSI({rsi_tf}) {rsi_tf_val:.0f} oversold (ไม่ช็อตก้นเหว)"}
    if direction == "buy" and rsi_tf_val > rsi_obt:
        log.info("ข้าม %s — RSI(%s) %.0f overbought (ไม่ long ยอดดอย)", exsym, rsi_tf, rsi_tf_val)
        return {"skipped": True, "exsym": exsym, "direction": direction,
                "reason": f"RSI({rsi_tf}) {rsi_tf_val:.0f} overbought (ไม่ long ยอดดอย)"}

    # ── MTF Alignment Filter — ไม่เข้าสวนเทรนด์ TF ใหญ่ (ลดไม้ "แดงยาวจน SL") ──
    # ปกติยกเว้น mean-reversion (vwap/rsi_div) ที่สวนเทรนด์โดยตั้งใจ
    # แต่ MTF_INCLUDE_MEANREV=true → บังคับให้ mean-reversion เคารพ MTF ด้วย
    #   (เช่น crypto เทรนด์แรง — RSI div/VWAP สวนเทรนด์ = แดงตั้งแต่เข้า → เปิดออปชันนี้กัน)
    if (cfg.get("USE_MTF_FILTER", "false").lower() in ("1", "true", "yes", "on")
            and bias.get("source", "") not in _OWN_TREND_SRC):
        _include_mr = cfg.get("MTF_INCLUDE_MEANREV", "false").lower() in ("1", "true", "yes", "on")
        _is_mr = bias.get("source", "") in _MEAN_REVERSION_SRC
        if _include_mr or not _is_mr:
            _mtf_tf = cfg.get("MTF_TF", "H1")
            _htf = _higher_tf_trend(exsym, _mtf_tf, mt5)
            if (direction == "buy" and _htf == "down") or (direction == "sell" and _htf == "up"):
                _mr_tag = " (รวม mean-rev)" if _is_mr else ""
                log.debug("ข้าม %s — สวนเทรนด์ %s (%s) [MTF filter%s]", exsym, _mtf_tf, _htf, _mr_tag)
                return {"skipped": True, "exsym": exsym, "direction": direction,
                        "reason": f"สวนเทรนด์ {_mtf_tf} ({_htf}) — MTF filter"}

    # ── Regime filter — "Avoid trading in the middle of a range" ─────────────
    # ADX+Choppiness+Market Structure บน H1 → ตลาด sideways = no-trade สำหรับ
    # กลยุทธ์ตามเทรนด์ (mean-reversion ยกเว้น — edge ของมันคือ range พอดี)
    # ต่างจาก MTF filter: MTF กัน "สวนเทรนด์" · regime กัน "ไม่มีเทรนด์ให้ตาม"
    if (cfg.get("USE_REGIME_FILTER", "false").lower() in ("1", "true", "yes", "on")
            and bias.get("source", "") not in _REGIME_EXEMPT_SRC):
        import regime as _regime
        _rg_tf = cfg.get("REGIME_TF", "H1")
        _rg_df = mt5.rates(exsym, _rg_tf, 120)
        _rg = _regime.classify(_rg_df,
                               adx_min=float(cfg.get("REGIME_ADX_MIN", "20") or "20"),
                               chop_max=float(cfg.get("REGIME_CHOP_MAX", "55") or "55"))
        if _rg["regime"] == "range":
            log.debug("ข้าม %s — ตลาด sideways (%s) [regime filter]", exsym, _rg["reason"])
            return {"skipped": True, "exsym": exsym, "direction": direction,
                    "reason": f"ตลาด sideways — {_rg['reason']} [regime]"}
        if ((_rg["regime"] == "trend_up" and direction == "sell")
                or (_rg["regime"] == "trend_down" and direction == "buy")):
            log.debug("ข้าม %s — สวน market structure (%s) [regime filter]", exsym, _rg["reason"])
            return {"skipped": True, "exsym": exsym, "direction": direction,
                    "reason": f"สวน structure {_rg['structure']} — {_rg['reason']} [regime]"}

    # ── Anti-chase / closed-bar guard — ไม่ไล่ราคาที่วิ่งไปไกลแล้วในแท่งนี้ ──
    # แท่ง forming ขยับจาก open เกิน CHASE_ATR_MULT×ATR ในทิศเทรด = ไล่ของแพง → เด้งกลับง่าย
    if cfg.get("REQUIRE_BAR_CLOSE", "false").lower() in ("1", "true", "yes", "on") and atr > 0:
        _chase = float(cfg.get("CHASE_ATR_MULT", "0.7") or "0.7")
        _bar_open = float(df["open"].iloc[-1])
        _moved = (spot - _bar_open) if direction == "buy" else (_bar_open - spot)
        if _moved > _chase * atr:
            log.debug("ข้าม %s — ไล่ราคา (แท่งวิ่ง %.1f×ATR > %.1f) [anti-chase]", exsym, _moved / atr, _chase)
            return {"skipped": True, "exsym": exsym, "direction": direction,
                    "reason": f"ไล่ราคา (แท่งวิ่ง {_moved / atr:.1f}×ATR เกิน {_chase})"}

    # ── Pro Scalping Filters (Kill Zone/Liquidity Sweep/Momentum/VWAP distance) ──
    # ใช้กับกลยุทธ์ scalp/สั้นเท่านั้น (ดู _SCALP_FILTER_SRC) · เปิดผ่าน USE_SCALP_FILTERS
    # ดึง M5 มาตรวจ (filter ออกแบบสำหรับ M5) · ไม่ผ่าน → ข้าม
    if (cfg.get("USE_SCALP_FILTERS", "false").lower() in ("1", "true", "yes", "on")
            and bias.get("source", "") in _SCALP_FILTER_SRC):
        _sf_tf = cfg.get("SCALP_FILTER_TF", "M5")
        _sf_df = mt5.rates(exsym, _sf_tf, int(cfg.get("SCALP_FILTER_BARS", "300") or "300"))
        if _sf_df is not None and len(_sf_df) >= 30:
            _sf_cfg = cfg
            if bias.get("source", "") in _MOMENTUM_EXEMPT:
                # ปิดเฉพาะ Momentum confirm ให้กลยุทธ์ที่มันขัดธรรมชาติ — filter อื่นยังตรวจปกติ
                # และลดเกณฑ์ score ลง 1 ตามสัดส่วน (ตัด filter ให้คะแนนออก 1 ตัว:
                # เดิม 2-จาก-3 → 1-จาก-2 · ไม่งั้นต้องผ่าน LiqSweep+VWAPdist ทั้งคู่ = แทบเป็นไปไม่ได้)
                _min_sc = max(1, int(cfg.get("SCALP_FILTER_MIN_SCORE", "2") or "2") - 1)
                _sf_cfg = {**cfg, "SCALP_FILTER_MOMENTUM": "false",
                           "SCALP_FILTER_MIN_SCORE": str(_min_sc)}
            _sf = scalp_filters.check_all_filters(_sf_df, direction, _sf_cfg, symbol=exsym,
                                                  category=_sym_cat, atr=atr)
            if not _sf.get("pass"):
                log.debug("ข้าม %s — %s [scalp filters %d/%d]", exsym, _sf.get("reason", ""),
                          _sf.get("score", 0), _sf.get("max_score", 0))
                return {"skipped": True, "exsym": exsym, "direction": direction,
                        "reason": _sf.get("reason", "ไม่ผ่าน scalp filters")}

    lb = int(cfg.get("SL_LOOKBACK", "20"))            # จำนวนแท่งหา swing
    mult = float(cfg.get("SL_ATR_MULT", "1.5"))       # กันชน ATR
    if direction == "buy":
        sl = min(float(df["low"].iloc[-lb:].min()), spot - mult * atr)
    else:
        sl = max(float(df["high"].iloc[-lb:].max()), spot + mult * atr)

    # 3-Bar Play: ตรวจบน TF เร็ว (เช่น M15) เพื่อจับโมเมนตัมไว — เสริม SL แคบกว่า ATR swing
    bp_tf = cfg.get("THREEBP_TF", entry_tf) or entry_tf
    bp_df = mt5.rates(exsym, bp_tf, 200) if bp_tf.upper() != entry_tf.upper() else df
    bp_atr = _atr(bp_df, 14) if (bp_df is not None and len(bp_df) >= 20) else atr
    tbp = (patterns.three_bar_play(bp_df, direction, bp_atr)
           if (bp_df is not None and len(bp_df) >= 5) else {"detected": False})
    if tbp.get("detected") and cfg.get("USE_3BP_SL", "true").lower() in ("1", "true", "yes", "on"):
        cand = tbp["sl"]
        side_ok = (cand < spot) if direction == "buy" else (cand > spot)
        if side_ok and abs(spot - cand) >= 0.3 * bp_atr:    # กัน SL แคบเกินจนโดน noise เขี่ย
            sl = cand

    # TP คำนวณสองโหมด:
    # 1) ATR-based (TP_ATR_MULT > 0): TP = entry ± ATR × multiplier — สมจริง ปรับตามความผันผวน
    # 2) R:R-based (TP_ATR_MULT = 0, default): TP = entry ± SL_dist × TP_RR (เดิม)
    # tp_rr ต้องกำหนดก่อนเสมอ — BRT/IBB/TLP ที่อยู่ด้านล่างต้องใช้ tp_rr ด้วย
    # ถ้า TP_ATR_MULT > 0 → ATR-based TP แต่ tp_rr ยังต้องมีค่าไว้เป็น fallback
    tp_rr = float(cfg.get("TP_RR", "2.0"))
    tp_atr_mult = float(cfg.get("TP_ATR_MULT", "0") or "0")
    if tp_atr_mult > 0 and atr > 0:
        tp = spot + tp_atr_mult * atr if direction == "buy" else spot - tp_atr_mult * atr
        log.debug("TP ATR-based %s: %.5f (%.1f×ATR %.5f)", exsym, tp, tp_atr_mult, atr)
    else:
        lv = risk.build_levels(spot, sl, tp_rr=tp_rr)
        tp = lv["tp"]
    # ต้นตำรับ 3BP: TP = ความยาวแท่ง1 (ถ้าเปิด USE_3BP_TP และยังให้ R:R คุ้ม >= MIN_RR)
    if tbp.get("detected") and cfg.get("USE_3BP_TP", "false").lower() in ("1", "true", "yes", "on"):
        b1 = tbp.get("bar1_range")
        if b1:
            tp_b1 = spot + b1 if direction == "buy" else spot - b1
            rr_b1 = risk.rr(spot, sl, tp_b1)
            if rr_b1 and rr_b1 >= float(cfg.get("MIN_RR", "1.5")):
                tp = tp_b1

    # Breakout & Retest (ตามเทรนด์) + แนวรับ-ต้าน (เตือน Gemini ไม่ long ชนต้าน/short ชนรับ)
    sr = patterns.support_resistance(df, atr=atr)
    brt = patterns.breakout_retest(df, direction, atr)
    if brt.get("detected") and cfg.get("USE_BRT_SL", "true").lower() in ("1", "true", "yes", "on"):
        cand = brt["sl"]
        side_ok = (cand < spot) if direction == "buy" else (cand > spot)
        if side_ok and abs(spot - cand) >= 0.3 * atr:    # SL ใต้/เหนือแนวที่เพิ่งเบรก
            sl = cand
            tp = risk.build_levels(spot, sl, tp_rr=tp_rr)["tp"]

    # Inside Bar Breakout + 2-Legged Pullback (entry TF) — confluence + SL ทางเลือก
    ibb = patterns.inside_bar_breakout(df, direction, atr)
    tlp = patterns.two_legged_pullback(df, direction, atr)
    for pat, flag in ((ibb, "USE_IBB_SL"), (tlp, "USE_TLP_SL")):
        if pat.get("detected") and pat.get("sl") is not None and cfg.get(flag, "true").lower() in ("1", "true", "yes", "on"):
            cand2 = pat["sl"]
            side_ok = (cand2 < spot) if direction == "buy" else (cand2 > spot)
            if side_ok and abs(spot - cand2) >= 0.3 * atr:
                sl = cand2
                tp = risk.build_levels(spot, sl, tp_rr=tp_rr)["tp"]

    # Take-profit ตาม % ราคา (ผู้ใช้กำหนด: ราคาขึ้นถึง N% → ขายเลย) — override TP เป็นระดับราคา N%
    tp_pct = float(cfg.get("TP_PRICE_PCT", "0") or "0")
    if tp_pct > 0 and not scalp:
        tp = spot * (1 + tp_pct / 100) if direction == "buy" else spot * (1 - tp_pct / 100)
    # ไม้ scalp (เช่น EMA+Stoch M15): ใช้ SL/TP ของกลยุทธ์เอง (ปิดไว rr สั้น ตั้งที่โบรก)
    # ไม่ผ่านกฎ TP +2% — ไม่งั้น scalp จะถือยาวกลายเป็นสวิง ผิดจาก backtest
    if scalp:
        sl = float(scalp["sl"])
        side_ok = (sl < spot) if direction == "buy" else (sl > spot)
        if (not side_ok) or abs(spot - sl) < 0.3 * atr:   # ราคาเลยจุด SL ไปแล้ว/แคบไป → ยกเลิก
            return {"skipped": True, "exsym": exsym, "direction": direction,
                    "reason": "scalp: ราคาเลยจุด SL"}
        if scalp.get("tp"):                       # ORB: TP เป็นระดับราคาสัมบูรณ์ (เท่าความกว้างกรอบ)
            tp = float(scalp["tp"])
        else:                                     # EMA+Stoch: TP = rr × ระยะ SL
            rd = abs(spot - sl)
            rr_s = float(scalp.get("rr", 1.8))
            tp = spot + rr_s * rd if direction == "buy" else spot - rr_s * rd
        tbp = brt = ibb = tlp = {"detected": False}

    # ── VPOC Filter + TP Zone ─────────────────────────────────────────────────
    # ใช้เฉพาะเทคนิค trend-following — กัน scalp/ORB ที่ SL/TP ตัวเองแน่นแล้วถูกแทรกแซง
    # Filter : ราคาใกล้ VPOC < VPOC_FILTER_ATR×ATR → ตลาดสมดุล ยังไม่มีทิศ → ลด lot
    # TP Zone: VPOC อยู่ข้างหน้า ≥ VPOC_MIN_RR×R → ใช้เป็น TP แทน fixed R:R
    vpoc_info: dict = {}
    near_vpoc: bool = False
    vpoc_tp_used: bool = False
    _vpoc_sources = {"supertrend", "halftrend", "utbot", "hybrid", "pa"}   # เทคนิค trend ที่ VPOC เหมาะ
    if bias.get("source") in _vpoc_sources and cfg.get("USE_VPOC", "true").lower() in ("1", "true", "yes", "on"):
        _vpoc_tf   = cfg.get("VPOC_TF", "M5")
        _vpoc_bars = int(cfg.get("VPOC_BARS", "288"))    # 288×M5 = 24h ครอบทุก session
        _df_vp = mt5.rates(exsym, _vpoc_tf, _vpoc_bars)
        if _df_vp is not None and len(_df_vp) >= 50:
            _vp = _scalp_module.vpoc(_df_vp)
            if _vp:
                vpoc_info = _vp
                _dist_atr = abs(spot - _vp["vpoc"]) / atr if atr > 0 else 99.0
                near_vpoc = _dist_atr < float(cfg.get("VPOC_FILTER_ATR", "0.5"))
                # TP Zone — VPOC ต้องอยู่ข้างหน้าในทิศที่เทรด และ R:R คุ้มค่า
                _vpoc_ahead = (_vp["vpoc"] > spot) if direction == "buy" else (_vp["vpoc"] < spot)
                if _vpoc_ahead:
                    _sl_dist   = abs(spot - sl)
                    _vpoc_dist = abs(_vp["vpoc"] - spot)
                    _vpoc_rr   = _vpoc_dist / _sl_dist if _sl_dist > 0 else 0.0
                    if _vpoc_rr >= float(cfg.get("VPOC_MIN_RR", "1.5")):
                        tp = _vp["vpoc"]          # override TP = VPOC (volume magnet)
                        vpoc_tp_used = True
                        log.info("VPOC TP %s %s → %.5f (R:R %.2f · VAH=%.5f VAL=%.5f)",
                                 exsym, direction, tp, _vpoc_rr, _vp["vah"], _vp["val"])

    rr_val = risk.rr(spot, sl, tp)

    # เกราะ spread: ข้ามถ้า spread กว้างเกิน (เทรดสั้น spread กว้าง = กินกำไร)
    # crypto spread กว้างกว่า FX/commodity ตามธรรมชาติ → ใช้ threshold แยก asset class
    spread = (px["ask"] - px["bid"]) if (px and px.get("ask") and px.get("bid")) else 0.0
    spread_pct = (spread / spot * 100) if spot else 0.0
    # spread threshold แยกตาม asset class — US stocks/indices spread ตามธรรมชาติกว้างกว่า FX
    if _sym_cat == "crypto":
        max_spread = float(cfg.get("MAX_SPREAD_PCT_CRYPTO", "1.0"))
    elif _sym_cat == "us_stock":
        max_spread = float(cfg.get("MAX_SPREAD_PCT_STOCK", "0.40"))      # US CFD spread ปกติ 0.05-0.15%
    elif _sym_cat == "us_index":
        max_spread = float(cfg.get("MAX_SPREAD_PCT_INDEX", "0.20"))      # US30/US500 spread ปกติ 0.03-0.10%
    elif _sym_cat == "index":
        # EU/Asia index (STOXX50/UK100/DAX) spread กว้างกว่า US index ตามธรรมชาติ
        max_spread = float(cfg.get("MAX_SPREAD_PCT_EU_INDEX",
                                   cfg.get("MAX_SPREAD_PCT_INDEX", "0.22")))
    elif _sym_cat == "commodity":
        max_spread = float(cfg.get("MAX_SPREAD_PCT_COMMODITY", "0.80"))  # Palladium/Platinum ~0.5-0.8% ปกติ
    else:
        max_spread = float(cfg.get("MAX_SPREAD_PCT", "0.15"))            # FX เท่านั้น (spread แคบ ~0.01-0.05%)
    if spread_pct > max_spread:
        log.debug("ข้าม %s — spread กว้าง %.3f%% > %.2f%% (%s)", exsym, spread_pct, max_spread, _sym_cat)
        return {"skipped": True, "exsym": exsym, "direction": direction,
                "reason": f"spread {spread_pct:.2f}% > {max_spread}%"}
    # หัก spread + ค่าคอม (ต่อรอบ) ออกจาก R:R → R:R สมจริง
    cost = spread + float(cfg.get("COMMISSION_PCT", "0") or "0") / 100 * spot
    rr_eff = ((abs(tp - spot) - cost) / (abs(spot - sl) + cost)) if (abs(spot - sl) + cost) > 0 else 0.0
    # R:R threshold แยกตามกลยุทธ์ — Hybrid-Pro มีหลาย filter กรองก่อนแล้ว (H1+M15+RSI+แท่ง)
    # → ผ่อน threshold ได้เพราะ signal quality สูงกว่า VWAP/EMA M5 ที่สัญญาณเยอะกว่า
    _source = bias.get("source", "")
    if _source == "hybrid":
        min_rr = float(cfg.get("MIN_RR_HYBRID", "1.30"))
    else:
        min_rr = float(cfg.get("MIN_RR", "1.5"))
    if rr_eff < min_rr:
        log.debug("ข้าม %s — R:R หลังหักต้นทุน 1:%.2f < ขั้นต่ำ 1:%.1f (%s)", exsym, rr_eff, min_rr, _source or "default")
        return {"skipped": True, "exsym": exsym, "direction": direction,
                "reason": f"R:R(หักต้นทุน) 1:{rr_eff:.2f} < 1:{min_rr:.1f}"}
    rr_val = round(rr_eff, 2)          # R:R ที่หักต้นทุน spread/คอมแล้ว (ใช้แสดง+เรียนรู้)

    cdl = candles.confirms(df, direction)
    vol = patterns.volume_entering(df)
    brk = patterns.breakout(df)
    struct = patterns.structure(df)

    # ── Require entry confirmation — ต้องมีหลักฐานยืนยันอย่างน้อย 1 ถึงเข้า ──
    # กันไม้ "เปล่า" (ไม่มีแท่งยืนยัน/volume/pattern) ที่มักเด้งสวนทันที
    # นับ: แท่งเทียนยืนยัน · volume เข้า · 3BP · BRT · IBB · 2-Leg (อย่างใดอย่างหนึ่ง)
    if cfg.get("REQUIRE_ENTRY_CONFIRM", "false").lower() in ("1", "true", "yes", "on"):
        _has_confirm = bool(cdl) or vol.get("entering") or tbp.get("detected") \
            or brt.get("detected") or ibb.get("detected") or tlp.get("detected")
        if not _has_confirm:
            log.info("ข้าม %s — ไม่มีแท่งยืนยัน/volume/pattern [require-confirm]", exsym)
            return {"skipped": True, "exsym": exsym, "direction": direction,
                    "reason": "ไม่มีแท่งยืนยัน/volume/pattern"}

    bal = account.get("balance", 0) or 0
    used_bal = bal if bal > 0 else float(cfg.get("TEST_BALANCE", "1000"))
    risk_pct = float(cfg.get("RISK_PCT_PER_TRADE", "1.0"))

    # ── Confluence boost (Confirmation mode) ──────────────────────────────────
    # หลายกลยุทธ์ entry เห็นพ้อง symbol+direction เดียวกัน = สัญญาณแข็งแรงกว่า
    # → เพิ่มความเสี่ยง/lot (cap ที่ MAX_RISK_PCT) · ไม่ลดจำนวนไม้ แค่ไม้ confluence ใหญ่ขึ้น
    _confl_srcs = [s for s in (bias.get("confluence") or []) if s]
    _confl_n = len(_confl_srcs)
    _confl_min = int(cfg.get("CONFLUENCE_MIN_COUNT", "2") or "2")
    confluence_boosted = False
    if (_confl_n >= _confl_min
            and cfg.get("USE_CONFLUENCE", "true").lower() in ("1", "true", "yes", "on")):
        _boost = float(cfg.get("CONFLUENCE_LOT_MULT", "1.5") or "1.5")
        # cap ที่ 97% ของเพดาน — เว้น headroom ให้ lot ปัดขึ้นได้โดยไม่โดน MAX_RISK_PCT guard เด้งทิ้ง
        _cap = float(cfg.get("MAX_RISK_PCT", "2.0")) * 0.97
        risk_pct = min(risk_pct * _boost, _cap)
        confluence_boosted = True
        log.info("🔗 Confluence %s %s — %d กลยุทธ์เห็นพ้อง (%s) → risk %.2f%%",
                 exsym, direction, _confl_n, "+".join(_confl_srcs), risk_pct)

    # ── Auto lot by edge ──────────────────────────────────────────────────────
    # ปรับ lot ตาม edge จริงของกลยุทธ์ (avg R-multiple จากผลเทรด) — edge ดี×ใหญ่ขึ้น · แย่×เล็กลง
    # cap ที่ MAX_RISK_PCT เสมอ · ปิดฟีเจอร์/ข้อมูลน้อย → multiplier = 1.0 (ไม่ปรับ)
    edge_mult = learn.edge_multiplier(_source, cfg)
    if edge_mult != 1.0:
        _cap2 = float(cfg.get("MAX_RISK_PCT", "2.0")) * 0.97   # headroom กัน guard เด้งทิ้ง
        risk_pct = min(risk_pct * edge_mult, _cap2)
        log.info("📊 Edge sizing %s (%s) — ×%.2f → risk %.2f%%", exsym, _source or "?", edge_mult, risk_pct)

    # โลหะมีค่า (XAU/XAG/XPT/XPD): SL กว้าง + contract ใหญ่ → เสี่ยง/ไม้โตกว่าสินทรัพย์อื่น ~5 เท่า
    # (ข้อมูลจริง 11 มิ.ย.: โลหะเสี่ยงเฉลี่ย $10 vs อื่น $1.8 · ขาดทุนโลหะ -$29 จาก -$38 ทั้งวัน)
    # เพดานแยก MAX_RISK_PCT_METAL (ดีฟอลต์ 1.0% = ครึ่งของเพดานปกติ) · 0=ปิด ใช้เพดานรวม
    _metal_cap = (float(cfg.get("MAX_RISK_PCT_METAL", "1.0") or "0")
                  if _u.startswith(("XAU", "XAG", "XPT", "XPD")) else 0.0)
    if _metal_cap > 0:
        risk_pct = min(risk_pct, _metal_cap * 0.97)   # headroom กัน lot ปัดขึ้นแล้วเด้ง guard

    sizing = mt5.lots_for_risk(exsym, used_bal, risk_pct, spot, sl)

    # VPOC Filter: ลด lot เมื่ออยู่ใน equilibrium zone (ราคาใกล้ VPOC — ยังไม่มีทิศชัด)
    if near_vpoc and sizing and sizing.get("lots", 0) > 0:
        _vp_reduce = float(cfg.get("VPOC_LOT_REDUCE", "0.4"))
        _sz_vp = mt5.lots_for_risk(exsym, used_bal, risk_pct * _vp_reduce, spot, sl)
        if _sz_vp and _sz_vp.get("lots", 0) > 0:
            sizing = _sz_vp
            log.info("VPOC Filter: ลด lot → %.2f lots (ใกล้ VPOC %.5f ห่าง %.2f×ATR)",
                     _sz_vp["lots"], vpoc_info["vpoc"],
                     abs(spot - vpoc_info["vpoc"]) / atr if atr > 0 else 0)

    # เกราะความเสี่ยง: ข้ามไม้ที่ความเสี่ยงจริง (หลังปัด lot ขั้นต่ำ) เกินเพดาน
    # (เคสพอร์ตเล็ก + SL กว้าง เช่นทองบนพอร์ต $500 → lot ขั้นต่ำเสี่ยงทะลุเป้า)
    max_risk_pct = float(cfg.get("MAX_RISK_PCT", "2.0"))
    if _metal_cap > 0:
        max_risk_pct = min(max_risk_pct, _metal_cap)   # โลหะใช้เพดานเข้มกว่า — lot ขั้นต่ำเสี่ยงเกิน → ข้าม
    if sizing and sizing.get("actual_pct") is not None and sizing["actual_pct"] > max_risk_pct:
        log.debug("ข้าม %s — เสี่ยงจริง %.1f%% เกินเพดาน %.1f%% (พอร์ตเล็ก/SL กว้างไปสำหรับตัวนี้)",
                  exsym, sizing["actual_pct"], max_risk_pct)
        return {"skipped": True, "exsym": exsym, "direction": direction,
                "reason": f"เสี่ยง {sizing['actual_pct']:.1f}% เกินเพดาน {max_risk_pct:.0f}%"}

    gate = risk.gate(rr_val=rr_val, min_rr=min_rr,   # ใช้ min_rr ที่แยกตามกลยุทธ์แล้ว
                     open_positions=0, max_positions=int(cfg.get("MAX_OPEN_POSITIONS", "5")),
                     day_loss_pct=0.0, max_daily_loss_pct=float(cfg.get("MAX_DAILY_LOSS_PCT", "4.0")))

    ctx = {
        "symbol": exsym, "direction": direction, "entry": round(spot, 5), "sl": round(sl, 5),
        "tp": round(tp, 5) if tp else None, "rr": round(rr_val, 2) if rr_val else None,
        "source": bias.get("source"),            # supertrend / hybrid / scalp / fx_orb
        "st_value": bias.get("st_value"),        # SuperTrend line value (ถ้ามี)
        "candles": [c["name"] for c in cdl], "volume_entering": vol.get("entering"),
        "volume_ratio": vol.get("ratio"), "breakout": brk.get("type") if brk else None,
        "three_bar_play": tbp.get("detected"), "breakout_retest": brt.get("detected"),
        "inside_bar_breakout": ibb.get("detected"), "two_leg_pullback": tlp.get("detected"),
        "near_resistance": sr.get("near_resistance"), "near_support": sr.get("near_support"),
        "structure": struct.get("label"), "risk_gate_ok": gate["ok"], "risk_gate_reasons": gate["reasons"],
        "vpoc": round(vpoc_info["vpoc"], 5) if vpoc_info else None,
        "near_vpoc": near_vpoc, "vpoc_tp_used": vpoc_tp_used,
        "confluence": _confl_srcs,               # กลยุทธ์ที่เห็นพ้อง (≥2 = สัญญาณแข็งแรง)
    }
    memory = learn.summary_for_ai(int(cfg.get("LEARN_MIN_SAMPLES", "10")))   # บทเรียนจากผลจริง
    verdict = gemini_gate.assess(ctx, cfg.get("GEMINI_API_KEY"), memory)

    # ปรับขนาดไม้ตามความมั่นใจ AI: enter=เต็ม · small/skip/manual=เล็กลง
    # (ไม้ที่ AI ขอระวัง (small/skip) ยังเทรดได้ แต่ลดความเสี่ยงลง — กันเด้งสวน)
    dec = verdict.get("decision", "manual")
    reduced = dec != "enter"
    if reduced and sizing and sizing.get("lots", 0) > 0:
        mult = float(cfg.get("SMALL_RISK_MULT", "0.5"))
        scaled = mt5.lots_for_risk(exsym, used_bal, risk_pct * mult, spot, sl)
        if scaled and scaled.get("lots", 0) > 0:
            sizing = scaled

    return {"exsym": exsym, "bias": bias, "direction": direction,
            "spot": spot, "sl": sl, "tp": tp, "rr": rr_val, "atr": atr, "candles": cdl, "vol": vol,
            "breakout": brk, "structure": struct, "three_bar": tbp, "brt": brt,
            "ibb": ibb, "tlp": tlp, "sr": sr, "scalp": scalp.get("tag") if scalp else None,
            "sizing": sizing, "gate": gate, "verdict": verdict, "used_balance": used_bal,
            "balance_is_test": bal <= 0, "reduced": reduced,
            "rsi_tf": round(rsi_tf_val, 1),  # RSI จาก TF ของเทคนิคจริง (ไม่ใช่ ENTRY_TF เสมอ)
            "vpoc_info": vpoc_info, "near_vpoc": near_vpoc, "vpoc_tp_used": vpoc_tp_used,
            "confluence": _confl_srcs, "confluence_boosted": confluence_boosted}


def format_ticket(t: dict) -> str:
    b = t["bias"]
    v = t["verdict"]
    d = v.get("decision", "manual")

    def fx(x):
        return f"{x:,.5f}".rstrip("0").rstrip(".") if x is not None else "—"

    lines = [f"📋 ใบสั่งเทรด — {t['exsym']}",
             f"ทิศ: {_DIR_TH.get(t['direction'], t['direction'])}   [AI: {_DECISION_TH.get(d, d)}"
             + (f" {v['confidence']}%" if v.get("confidence") is not None else "") + "]"]
    # แหล่งสัญญาณ (SuperTrend / Hybrid-Pro / EMA+Stoch / FX ORB)
    _src_map = {"supertrend": "📈 SuperTrend", "halftrend": "〰️ HalfTrend",
                "utbot": "🤖 UT Bot", "hybrid": "🔀 Hybrid-Pro",
                "scalp": "⚡ EMA+Stoch", "fx_orb": "🌅 FX ORB",
                "pa": "📐 Price Action"}
    src = b.get("source", "")
    src_txt = _src_map.get(src, f"📊 {src}" if src else "📊 สัญญาณ")
    if b.get("st_value"):
        src_txt += f" · ST={b['st_value']}"
    lines.append(src_txt)
    # Confluence — หลายกลยุทธ์เห็นพ้อง symbol+direction เดียวกัน (lot ใหญ่ขึ้น)
    _confl = t.get("confluence") or []
    if t.get("confluence_boosted") and len(_confl) >= 2:
        _confl_lbl = " + ".join(_src_map.get(s, s) for s in _confl)
        lines.append(f"🔗 Confluence ×{len(_confl)}: {_confl_lbl} — เพิ่ม lot")
    # ยืนยัน Part 2
    conf = []
    if t["candles"]:
        conf.append("🕯️ " + ", ".join(c["name"] for c in t["candles"][:2]))
    if t["vol"].get("entering"):
        conf.append(f"📊 วอลุ่มเข้า {t['vol'].get('ratio')}×")
    if t["breakout"]:
        conf.append("🚀 เบรกกรอบ")
    if t.get("three_bar", {}).get("detected"):
        conf.append("🔥 3-Bar Play")
    if t.get("brt", {}).get("detected"):
        conf.append("🔁 Breakout-Retest")
    if t.get("ibb", {}).get("detected"):
        conf.append("📦 Inside-Bar Breakout")
    if t.get("tlp", {}).get("detected"):
        conf.append("📐 2-Leg Pullback")
    _struct_label = t["structure"].get("label")
    if _struct_label:
        conf.append(_struct_label)
    lines.append("ยืนยัน: " + " · ".join(c for c in conf if c))
    _sr = t.get("sr") or {}
    if t["direction"] == "buy" and _sr.get("near_resistance"):
        lines.append("⚠️ ใกล้แนวต้าน — ระวังเด้งลง")
    elif t["direction"] == "sell" and _sr.get("near_support"):
        lines.append("⚠️ ใกล้แนวรับ — ระวังเด้งขึ้น")
    # แผนเทรด
    lines.append("─── แผนเทรด ───")
    lines.append(f"🎯 Entry: {fx(t['spot'])}")
    if t["rr"]:
        lines.append(f"🛡️ SL: {fx(t['sl'])}   🎯 TP: {fx(t['tp'])}   R:R {t['rr']:.2f}")
    else:
        lines.append(f"🛡️ SL: {fx(t['sl'])}")
    _vpi = t.get("vpoc_info") or {}
    if t.get("vpoc_tp_used") and _vpi:
        lines.append(f"🧲 VPOC TP {fx(_vpi['vpoc'])} · VAH {fx(_vpi['vah'])} · VAL {fx(_vpi['val'])}")
    elif t.get("near_vpoc") and _vpi:
        lines.append(f"⚠️ ใกล้ VPOC {fx(_vpi['vpoc'])} — ลด lot อัตโนมัติ (ตลาดยังสมดุล)")
    if t["sizing"]:
        sz = t["sizing"]
        baltxt = f"${t['used_balance']:,.0f}" + ("(ทดสอบ)" if t["balance_is_test"] else "")
        pct = (f" = {sz['actual_pct']}% ของพอร์ต {baltxt}" if sz.get("actual_pct") is not None
               else f" (พอร์ต {baltxt})")
        lines.append(f"💼 พอร์ตจริง: {baltxt}")
        lines.append(f"📦 Lot: {sz['lots']} · เสี่ยงจริง ${sz['risk_money']}{pct}")
    if v.get("risks"):
        lines.append("⚠️ ความเสี่ยง: " + " · ".join(v["risks"][:3]))
    if v.get("reason"):
        lines.append("🤖 " + v["reason"])
    lines.append("👉 กดเองใน MT5 · ℹ️ ไม่ใช่คำแนะนำลงทุน")
    return "\n".join(lines)
