"""
part2_mt5/manage.py — จัดการ position แบบ adaptive (เรียกทุก ๆ ไม่กี่วิใน loop)

กฎ (แปลงจาก "feeling" 4 ข้อ → วัดได้):
  1. TP ทันที เมื่อกำไรถึง HARD_TP_R (default 2.5R)
  2. Partial close 50% เมื่อถึง PARTIAL_AT_R (default 1.0R) — เก็บกำไรก้อนแรก
  3. เลื่อน SL เท่าทุน เมื่อถึง BREAKEVEN_AT_R (default 1.5R) — แยกจาก partial
     → ให้ราคา "หายใจ" ระหว่าง 1R-1.5R โดยไม่บังทุนเร็วเกินไป
  4. Trailing SL (ATR-aware) เริ่มหลัง breakeven — ล็อกกำไรตามราคา
  5. (optional) Reversal exit: ถ้ากำไรอยู่แล้วเจอ reversal candle M5 → ปิดก่อนชน SL

ปลอดภัย: SL/TP จริงถูกเซ็ตใน MT5 → ถึงบอท/เน็ตล่ม MT5 ก็ยังตัดให้ที่ SL
จัดการเฉพาะไม้ของ Part 2 (magic 260605)
"""
from __future__ import annotations
import logging

import execute

log = logging.getLogger("part2.manage")
_MAGIC = 260605
_state: dict = {}        # ticket → {"partial_done": bool, "breakeven_done": bool, "risk": float}
_atr_cache: dict = {}    # sym → (atr_value, timestamp) — กันเรียก MT5 ซ้ำทุก loop


def _current_atr(sym: str, tf_str: str = "M15", n: int = 14) -> float:
    """ATR ปัจจุบันบน TF ที่กำหนด — cache 60 วิต่อ symbol"""
    import time as _t, MetaTrader5 as m5, numpy as np
    now = _t.time()
    key = (sym, tf_str)
    cached = _atr_cache.get(key)
    if cached and now - cached[1] < 60:
        return cached[0]
    _tf_map = {
        "M1": m5.TIMEFRAME_M1, "M5": m5.TIMEFRAME_M5,
        "M15": m5.TIMEFRAME_M15, "H1": m5.TIMEFRAME_H1,
    }
    tf = _tf_map.get(tf_str.upper(), m5.TIMEFRAME_M15)
    try:
        rates = m5.copy_rates_from_pos(sym, tf, 0, n + 2)
        if rates is None or len(rates) < 5:
            return 0.0
        h = np.array([r["high"] for r in rates], dtype=float)
        l_arr = np.array([r["low"] for r in rates], dtype=float)
        c = np.array([r["close"] for r in rates], dtype=float)
        pc = np.roll(c, 1); pc[0] = c[0]
        tr = np.maximum(h - l_arr, np.maximum(np.abs(h - pc), np.abs(l_arr - pc)))
        atr = float(tr[-n:].mean())
        _atr_cache[key] = (atr, now)
        return atr
    except Exception:  # noqa: BLE001
        return 0.0


def _round_vol(vol: float, sym: str) -> float:
    import MetaTrader5 as m5
    info = m5.symbol_info(sym)
    if not info:
        return 0.0
    step = info.volume_step or 0.01
    v = round(vol / step) * step
    return round(v, 2) if v >= info.volume_min else 0.0


def _has_reversal_candle(sym: str, direction: str, tf_str: str = "M5") -> bool:
    """ตรวจ reversal candle บน TF ที่กำหนด
    direction = ทิศของ position (buy → ตรวจ bearish reversal, sell → bullish reversal)
    คืน True ถ้าเจอสัญญาณกลับตัว — ใช้ปิดไม้ก่อนกำไรหายไปหมด"""
    import MetaTrader5 as m5, numpy as np
    _tf_map = {
        "M1": m5.TIMEFRAME_M1, "M5": m5.TIMEFRAME_M5,
        "M15": m5.TIMEFRAME_M15, "H1": m5.TIMEFRAME_H1,
    }
    tf = _tf_map.get(tf_str.upper(), m5.TIMEFRAME_M5)
    try:
        rates = m5.copy_rates_from_pos(sym, tf, 0, 4)
        if rates is None or len(rates) < 3:
            return False
        import pandas as pd
        df = pd.DataFrame(rates)
        df.columns = [c.lower() for c in df.columns]
        if "tick_volume" in df.columns:
            df = df.rename(columns={"tick_volume": "volume"})
        import candles as cdl
        # ตรวจ reversal ฝั่งตรงข้ามกับ position (buy → ตรวจ bearish)
        opp = "sell" if direction == "buy" else "buy"
        patterns = cdl.confirms(df, opp)
        # กรอง: เอาเฉพาะ strength 2 (engulfing, pin bar, shooting star, marubozu — ไม่เอา doji/inside)
        strong = [p for p in patterns if p.get("strength", 0) >= 2]
        return len(strong) > 0
    except Exception:  # noqa: BLE001
        return False


def _trend_against(sym: str, direction: str, tf_str: str = "M15") -> bool:
    """Multi-signal exit: True ถ้า SuperTrend บน TF นี้ 'พลิกสวนทาง' position แล้ว
    direction = ทิศของ position (buy/sell) · ใช้ปิดไม้เมื่อเทคนิคเทรนด์เปลี่ยนข้าง
    (ต่างจาก reversal candle ที่ดูแค่แท่งเดียว — อันนี้ดูทิศเทรนด์ทั้งระบบ)"""
    import MetaTrader5 as m5
    _tf_map = {
        "M1": m5.TIMEFRAME_M1, "M5": m5.TIMEFRAME_M5,
        "M15": m5.TIMEFRAME_M15, "H1": m5.TIMEFRAME_H1,
    }
    tf = _tf_map.get(tf_str.upper(), m5.TIMEFRAME_M15)
    try:
        rates = m5.copy_rates_from_pos(sym, tf, 0, 60)
        if rates is None or len(rates) < 30:
            return False
        import pandas as pd, scalp
        df = pd.DataFrame(rates)
        df.columns = [c.lower() for c in df.columns]
        res = scalp.supertrend(df)
        if res.get("st") is None or len(res["direction"]) < 2:
            return False
        # ใช้แท่งที่ "ปิดแล้ว" ([-2]) ไม่ใช่แท่งกำลังก่อตัว ([-1]) — กัน flip หลอกกลางแท่ง
        # (แท่ง forming แกว่งได้ ราคาอาจเด้งกลับก่อนปิด → ปิดไม้กำไรก่อนเวลาโดยไม่จำเป็น)
        cur_dir = int(res["direction"][-2])     # +1 = ขาขึ้น (buy) · -1 = ขาลง (sell)
        # position buy → เทรนด์ต้องเป็น -1 (ลง) ถึงนับว่าสวน · sell → เทรนด์เป็น +1 (ขึ้น)
        return (direction == "buy" and cur_dir == -1) or (direction == "sell" and cur_dir == 1)
    except Exception:  # noqa: BLE001
        return False


def manage_positions(cfg: dict, balance: float = 0) -> None:
    """จัดการไม้แบบ R-multiple — แยก partial close จาก breakeven trigger:
      ถึง +PARTIAL_AT_R  → ปิดบางส่วน (เก็บกำไรก้อนแรก)
      ถึง +BREAKEVEN_AT_R → เลื่อน SL เท่าทุน (ให้ราคาหายใจ 1R-1.5R ก่อน)
      หลัง breakeven     → trailing SL ตาม ATR (ล็อกกำไรที่เหลือ)
      USE_REVERSAL_EXIT  → ถ้ากำไรอยู่แล้วเจอ reversal candle → ปิดก่อนชน SL"""
    import MetaTrader5 as m5
    poss = m5.positions_get()
    if not poss:
        _state.clear()
        return

    partial_at  = float(cfg.get("PARTIAL_AT_R",   "1.0"))   # ปิดบางส่วนเมื่อกำไรถึง 1R
    breakeven_at = float(cfg.get("BREAKEVEN_AT_R", "1.5"))  # เลื่อน SL เท่าทุนที่ 1.5R (แยกจาก partial)
    hard_r      = float(cfg.get("HARD_TP_R",       "2.5"))  # ปิดทั้งหมดเมื่อกำไรถึง 2.5R
    ratio       = float(cfg.get("PARTIAL_RATIO",   "0.5"))  # ปิดบางส่วน 50%
    tfac        = float(cfg.get("TRAIL_FACTOR",    "0.7"))  # ระยะ trailing = max(R, ATR) × factor
    tp_pct      = float(cfg.get("TP_PRICE_PCT",    "0") or "0")  # TP ตาม % (0 = ไม่ใช้)

    # Time stop — "ไม้ที่ไม่วิ่งเร็ว = ไม้ผิด" (กฎ scalper มืออาชีพ)
    # ⚠️ ขัดกฎ "ขาดทุนไม่ปิด" โดยเจตนา → opt-in เท่านั้น แนะนำทดสอบบน demo ก่อน
    use_time_stop = cfg.get("USE_TIME_STOP", "false").lower() in ("1", "true", "yes", "on")
    ts_minutes    = float(cfg.get("TIME_STOP_MIN", "45"))       # อายุไม้ขั้นต่ำก่อนตัดสิน
    ts_below_r    = float(cfg.get("TIME_STOP_BELOW_R", "0.0"))  # ปิดถ้า R ปัจจุบัน < ค่านี้

    # Reversal exit — ปิดก่อนชน SL ถ้าเจอ candle กลับตัวและยังกำไรอยู่
    use_rev_exit  = cfg.get("USE_REVERSAL_EXIT", "false").lower() in ("1", "true", "yes", "on")
    rev_min_r     = float(cfg.get("REVERSAL_EXIT_MIN_R", "0.5"))   # min R ก่อนเช็ค reversal
    rev_tf        = cfg.get("REVERSAL_EXIT_TF",   "M5")            # TF ตรวจ reversal candle

    # Multi-signal exit — ปิดเมื่อ SuperTrend (เทคนิคเทรนด์) พลิกสวนทาง position
    # ต่างจาก reversal candle: อันนี้ดู "ทิศเทรนด์ทั้งระบบ" ไม่ใช่แค่แท่งเดียว
    use_multi_exit = cfg.get("USE_MULTI_SIGNAL_EXIT", "false").lower() in ("1", "true", "yes", "on")
    multi_min_r    = float(cfg.get("MULTI_EXIT_MIN_R", "0.3"))     # min R ก่อนเช็ค (กันออกเร็วหลังเพิ่งเข้า)
    multi_tf       = cfg.get("MULTI_EXIT_TF", "M15")               # TF ตรวจ SuperTrend flip

    # 0) Flat ก่อนตลาดปิด — ปิดไม้ที่ตลาดใกล้ปิด (ไม่รวม crypto)
    # ⚠️ กฎ "ขาดทุนไม่ปิด" ถูกยกเลิกโดยผู้ใช้ 12 มิ.ย. 2026 จากหลักฐาน 134 ไม้:
    #   ไม้กำไรโดนปิดก่อนตลาดปิดเฉลี่ย +0.8R แต่ไม้ขาดทุนถือข้ามคืนโดน gap -1.9 ถึง -2.8R
    #   (GOOGL -2.76R/18ชม. · NKE -1.96R/22ชม. · INTC -1.93R/15ชม.) = ตัดกำไร-เลี้ยงขาดทุน
    # → ดีฟอลต์ใหม่: flat ทุกไม้ทั้งกำไร/ขาดทุน (CLOSE_ALL_BEFORE_MARKET_CLOSE=false เพื่อกลับพฤติกรรมเดิม)
    if cfg.get("CLOSE_BEFORE_MARKET_CLOSE", "true").lower() in ("1", "true", "yes", "on"):
        import market_hours
        buf = int(cfg.get("MARKET_CLOSE_BUFFER_MIN", "20"))
        closing = [p for p in poss if p.magic == _MAGIC and market_hours.closing_soon(p.symbol, buf)]
        if closing:
            import journal
            day_pnl = journal.today_pnl()
            _close_all = cfg.get("CLOSE_ALL_BEFORE_MARKET_CLOSE", "true").lower() in ("1", "true", "yes", "on")
            # โหมดเดิม (CLOSE_ALL=false): ปิดเฉพาะไม้กำไร · ไม้ขาดทุนปิดเฉพาะวันเขียว+เปิด flag
            _close_loser_green = cfg.get("CLOSE_LOSERS_ON_GREEN_DAY", "false").lower() in ("1", "true", "yes", "on")
            for p in closing:
                if _close_all or p.profit > 0 or (_close_loser_green and day_pnl > 0):
                    if execute.close_position(p).get("ok"):
                        log.info("🔔 ตลาดใกล้ปิด → flat %s (ไม้ $%.2f · พอร์ตวันนี้ $%.2f)",
                                 p.symbol, p.profit, day_pnl)
                        _state.pop(p.ticket, None)
            poss = m5.positions_get() or ()

    # 0b) FX Session Mode — ปิดไม้ FX ทั้งหมดเมื่อเข้าช่วงตลาด US (รวม buffer ก่อนเปิด)
    # ⚠️ ข้อยกเว้นกฎ "ขาดทุนไม่ปิด" โดยคำสั่งผู้ใช้ชัดเจน (10 มิ.ย. 2026):
    # FX เล่นเฉพาะนอกเวลาตลาด US และต้อง flat ก่อน US เปิด — กำไร/ขาดทุนปิดหมด
    # ไม่ถือข้าม volatility ช่วง US · เช็คตลอดช่วง US (idempotent — เก็บไม้ตกค้างด้วย)
    if cfg.get("FX_SESSION_MODE", "false").lower() in ("1", "true", "yes", "on"):
        import market_hours
        _fx_buf = int(cfg.get("FX_FLATTEN_BEFORE_US_MIN", "30") or "30")
        if market_hours.in_us_session(_fx_buf):
            fx_open = [p for p in poss
                       if p.magic == _MAGIC and market_hours.category(p.symbol) == "fx"]
            for p in fx_open:
                if execute.close_position(p).get("ok"):
                    log.info("🕗 ตลาด US ใกล้เปิด/เปิดอยู่ → flat FX %s (ไม้ $%.2f · กฎ FX session)",
                             p.symbol, p.profit)
                    _state.pop(p.ticket, None)
            if fx_open:
                poss = m5.positions_get() or ()

    alive = set()
    for p in poss:
        if p.magic != _MAGIC:
            continue
        alive.add(p.ticket)
        tick = m5.symbol_info_tick(p.symbol)
        if not tick:
            continue
        is_buy = p.type == m5.POSITION_TYPE_BUY
        cur = tick.bid if is_buy else tick.ask         # ราคาที่ปิดไม้ได้จริง

        # ── TP ตาม % ราคา ─────────────────────────────────────────────────────
        if tp_pct > 0:
            pchg = ((cur - p.price_open) if is_buy else (p.price_open - cur)) / p.price_open * 100
            if pchg >= tp_pct:
                execute.close_position(p)
                log.info("🎯 +%.2f%% ราคา (เป้า %.1f%%) → ปิด %s (#%s)", pchg, tp_pct, p.symbol, p.ticket)
                _state.pop(p.ticket, None)
                continue

        # ── State tracking ──────────────────────────────────────────────────
        st = _state.setdefault(p.ticket, {
            "partial_done": False,    # ปิดบางส่วนแล้ว (PARTIAL_AT_R)
            "breakeven_done": False,  # เลื่อน SL เท่าทุนแล้ว (BREAKEVEN_AT_R)
            "risk": abs(p.price_open - p.sl) if p.sl else None,
        })
        # backward compat: migrate state เก่าที่มีแค่ "partial"
        if "partial" in st and "partial_done" not in st:
            was_partial = st.pop("partial")
            st["partial_done"] = was_partial
            st["breakeven_done"] = was_partial   # ถ้าเคย partial แล้ว ถือว่า breakeven ด้วย
        if st["risk"] is None and p.sl:
            st["risk"] = abs(p.price_open - p.sl)
        R = st["risk"]
        if not R or R <= 0:          # ไม่รู้ความเสี่ยงเริ่มต้น → ข้าม
            continue
        rmult = ((cur - p.price_open) if is_buy else (p.price_open - cur)) / R

        # ── 1a) Time stop (optional) — ไม้ที่อายุเกินกำหนดแล้วยังไม่ไปไหน ──
        # ใช้ tick.time − p.time (server epoch ทั้งคู่ → timezone offset หักล้างกัน)
        # ไม้ที่ partial แล้ว = พิสูจน์ตัวเองแล้ว ไม่โดน time stop
        if use_time_stop and not st["partial_done"]:
            age_min = (tick.time - p.time) / 60.0
            if age_min >= ts_minutes and rmult < ts_below_r:
                if execute.close_position(p).get("ok"):
                    log.info("⏱️ Time stop: %.0f นาทียัง %.2fR (< %.1fR) → ปิด %s (#%s)",
                             age_min, rmult, ts_below_r, p.symbol, p.ticket)
                    _state.pop(p.ticket, None)
                    continue

        # ── 1) HARD TP ceiling ───────────────────────────────────────────────
        if tp_pct <= 0 and rmult >= hard_r:
            execute.close_position(p)
            log.info("🎯 +%.2fR (เพดาน %.1fR) → ปิด %s (#%s)", rmult, hard_r, p.symbol, p.ticket)
            _state.pop(p.ticket, None)
            continue

        # ── 2) Partial close ที่ PARTIAL_AT_R (ล็อกกำไรก้อนแรก) ───────────
        if not st["partial_done"] and rmult >= partial_at:
            if tp_pct <= 0:          # โหมด R-multiple → ปิดบางส่วนจริง
                half = _round_vol(p.volume * ratio, p.symbol)
                if 0 < half < p.volume:
                    if execute.close_position(p, half).get("ok"):
                        log.info("💰 ปิดบางส่วน %.2f lot ที่ +%.2fR %s (SL ยังที่เดิม รอ %.1fR ค่อยบังทุน)",
                                 half, rmult, p.symbol, breakeven_at)
                        st["partial_done"] = True   # มาร์คเฉพาะตอนปิดสำเร็จ
                    # โบรกปฏิเสธ (retcode != 10009) → partial_done คง False → ลองใหม่รอบหน้า
                    # กันบั๊ก: ถ้ามาร์คทั้งที่ปิดไม่ติด บอทจะเลื่อน SL เท่าทุนทั้งที่ยังไม่ได้ล็อกกำไร
                else:
                    # ไม้เล็กเกินจะแบ่ง (half ปัด = 0 หรือ ≥ volume) → ข้าม partial ไปเลย
                    # ไม่งั้นค้างที่ partial_done=False ตลอด → breakeven/trailing ไม่ทำงาน
                    st["partial_done"] = True
            else:                    # โหมด price-% TP: ไม่ปิดบางส่วนจริง แค่มาร์คผ่าน step นี้
                st["partial_done"] = True
            # *** ไม่ขยับ SL ทันที — รอถึง BREAKEVEN_AT_R ก่อน ให้ราคามีพื้นที่หายใจ ***
            continue

        # ── 3) เลื่อน SL เท่าทุน ที่ BREAKEVEN_AT_R (แยกจาก partial) ─────
        if st["partial_done"] and not st["breakeven_done"] and rmult >= breakeven_at:
            if execute.modify_sltp(p.ticket, p.price_open, p.tp):
                log.info("🛡️ เลื่อน SL เท่าทุน (+%.2fR ≥ %.1fR) %s — trailing จะเริ่มหลังนี้",
                         rmult, breakeven_at, p.symbol)
            st["breakeven_done"] = True
            continue

        # ── 4) Reversal exit (optional) — ปิดก่อนชน SL ถ้าเจอ candle กลับตัว ─
        if (use_rev_exit and st["partial_done"] and rmult >= rev_min_r
                and not st.get("rev_checked_bar")):
            direction = "buy" if is_buy else "sell"
            if _has_reversal_candle(p.symbol, direction, rev_tf):
                if execute.close_position(p).get("ok"):
                    log.info("🔄 Reversal candle %s (+%.2fR) → ปิดก่อนกำไรหาย %s (#%s)",
                             rev_tf, rmult, p.symbol, p.ticket)
                    _state.pop(p.ticket, None)
                    continue
            # throttle: เช็ค reversal แค่ครั้งเดียวต่อแท่ง (กันยิงซ้ำ)
            import MetaTrader5 as _m5
            _last = _m5.copy_rates_from_pos(p.symbol, _m5.TIMEFRAME_M5, 0, 1)
            if _last is not None and len(_last) > 0:
                st["rev_checked_bar"] = int(_last[0]["time"])
        elif use_rev_exit and st.get("rev_checked_bar"):
            # รีเซ็ต flag เมื่อขึ้นแท่งใหม่
            import MetaTrader5 as _m5
            _last = _m5.copy_rates_from_pos(p.symbol, _m5.TIMEFRAME_M5, 0, 1)
            if _last is not None and len(_last) > 0:
                if int(_last[0]["time"]) != st["rev_checked_bar"]:
                    st.pop("rev_checked_bar", None)

        # ── 4b) Multi-signal exit (optional) — ปิดเมื่อ SuperTrend พลิกสวนทาง ─
        # เข้าด้วยเทคนิคไหนก็ได้ แต่ถ้าเทรนด์ระบบ (SuperTrend) เปลี่ยนข้าง → ออกตามสัญญาณใหม่
        # throttle ต่อแท่ง (กันเรียก MT5/คำนวณ SuperTrend ซ้ำทุกวิ)
        if use_multi_exit and rmult >= multi_min_r:
            import MetaTrader5 as _m5
            _bar = _m5.copy_rates_from_pos(p.symbol, _m5.TIMEFRAME_M5, 0, 1)
            _bar_t = int(_bar[0]["time"]) if (_bar is not None and len(_bar) > 0) else 0
            if st.get("multi_checked_bar") != _bar_t:        # แท่งใหม่ → เช็คได้อีกครั้ง
                st["multi_checked_bar"] = _bar_t
                direction = "buy" if is_buy else "sell"
                if _trend_against(p.symbol, direction, multi_tf):
                    if execute.close_position(p).get("ok"):
                        log.info("🔀 Multi-signal exit: SuperTrend(%s) พลิกสวน (+%.2fR) → ปิด %s (#%s)",
                                 multi_tf, rmult, p.symbol, p.ticket)
                        _state.pop(p.ticket, None)
                        continue

        # ── 5) Trailing SL — เริ่มหลัง breakeven เท่านั้น ─────────────────
        # dist = max(R×factor, ATR×factor) ป้องกัน noise ในตลาดผันผวน
        # US stocks/indices: tick size ใหญ่กว่า FX/commodity → ใช้ factor สูงกว่า
        # (กัน retcode 10025 "No changes" เมื่อ step เล็กกว่า minimum tick)
        if tp_pct <= 0 and st["breakeven_done"]:
            import market_hours as _mh
            _sym_cat = _mh.category(p.symbol)
            _effective_tfac = (float(cfg.get("TRAIL_FACTOR_US_STOCK", "1.2"))
                               if _sym_cat in ("us_stock", "us_index") else tfac)
            atr_now = _current_atr(p.symbol, "M15")
            dist = max(R * _effective_tfac, atr_now * _effective_tfac) if atr_now > 0 else R * _effective_tfac
            eps = cur * 1e-6
            if is_buy:
                new_sl = cur - dist
                if new_sl > (p.sl or 0) + eps:
                    execute.modify_sltp(p.ticket, new_sl, p.tp)
            else:
                new_sl = cur + dist
                if p.sl == 0 or new_sl < p.sl - eps:
                    execute.modify_sltp(p.ticket, new_sl, p.tp)

    # ── ล้าง state ของไม้ที่ปิดไปแล้ว ──────────────────────────────────────
    for t in list(_state):
        if t not in alive:
            _state.pop(t, None)
