"""
part2_mt5/manage.py — จัดการ position แบบ adaptive (เรียกทุก ๆ ไม่กี่วิใน loop)

กฎ (แปลงจาก "feeling" 3 ข้อ → วัดได้):
  1. เพดาน: กำไรถึง HARD_TP_PCT (2%) → ปิดทันที (ฟันเร็วตอนพุ่ง)
  2. ถึง PARTIAL_AT_PCT (0.5%) ครั้งแรก → ปิดบางส่วน (ล็อกกำไร) + เลื่อน SL เท่าทุน (worst case = เสมอ)
  3. หลังจากนั้น → trailing SL ตามราคา (ราคาย่อ = MT5 ปิดเก็บใกล้ยอดเอง)

ปลอดภัย: trailing ทำผ่าน "SL จริงใน MT5" → ถึงบอท/เน็ตล่ม MT5 ก็ยังปิดให้ที่ SL
จัดการเฉพาะไม้ของ Part 2 (magic 260605)
"""
from __future__ import annotations
import logging

import execute

log = logging.getLogger("part2.manage")
_MAGIC = 260605
_state: dict = {}        # ticket -> {"partial": bool, "risk": float}
_atr_cache: dict = {}    # sym → (atr_value, timestamp) — กันเรียก MT5 ซ้ำทุก loop


def _current_atr(sym: str, n: int = 14) -> float:
    """ATR ปัจจุบัน (M15, True Range) — adaptive trailing SL ตามความผันผวนจริง
    cache 60 วิ ต่อ symbol เพื่อลด MT5 API call"""
    import time as _t
    now = _t.time()
    cached = _atr_cache.get(sym)
    if cached and now - cached[1] < 60:
        return cached[0]
    try:
        import MetaTrader5 as m5
        import numpy as np
        rates = m5.copy_rates_from_pos(sym, m5.TIMEFRAME_M15, 0, n + 2)
        if rates is None or len(rates) < 5:
            return 0.0
        h = np.array([r["high"] for r in rates], dtype=float)
        l_arr = np.array([r["low"] for r in rates], dtype=float)
        c = np.array([r["close"] for r in rates], dtype=float)
        pc = np.roll(c, 1); pc[0] = c[0]
        tr = np.maximum(h - l_arr, np.maximum(np.abs(h - pc), np.abs(l_arr - pc)))
        atr = float(tr[-n:].mean())
        _atr_cache[sym] = (atr, now)
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


def manage_positions(cfg: dict, balance: float = 0) -> None:
    """จัดการไม้แบบ R-multiple (R = ระยะ SL เริ่มต้น) — สม่ำเสมอทุกขนาดไม้:
      ถึง +PARTIAL_AT_R → ปิดบางส่วน + เลื่อน SL เท่าทุน · trail ตาม R · ถึง +HARD_TP_R → ปิดทันที
    (balance ไม่ใช้แล้ว เก็บไว้เพื่อความเข้ากันได้กับตัวเรียก)"""
    import MetaTrader5 as m5
    poss = m5.positions_get()
    if not poss:
        _state.clear()
        return
    partial_at = float(cfg.get("PARTIAL_AT_R", "1.0"))
    hard_r = float(cfg.get("HARD_TP_R", "2.5"))
    ratio = float(cfg.get("PARTIAL_RATIO", "0.5"))
    tfac = float(cfg.get("TRAIL_FACTOR", "0.7"))

    tp_pct = float(cfg.get("TP_PRICE_PCT", "0") or "0")   # ปิดเมื่อราคาขยับถึง N% (0 = ไม่ใช้ ใช้ R-based)

    # 0) TP ก่อนตลาดปิด — ปิดไม้ที่ตลาดใกล้ปิด ถ้า "ไม้กำไร" หรือ "พอร์ตวันนี้เขียว" (ไม่รวม crypto 24ชม.)
    if cfg.get("CLOSE_BEFORE_MARKET_CLOSE", "true").lower() in ("1", "true", "yes", "on"):
        import market_hours
        buf = int(cfg.get("MARKET_CLOSE_BUFFER_MIN", "20"))
        closing = [p for p in poss if p.magic == _MAGIC and market_hours.closing_soon(p.symbol, buf)]
        if closing:
            import journal
            day_pnl = journal.today_pnl()
            for p in closing:
                if p.profit > 0 or day_pnl > 0:           # ไม้กำไร หรือ พอร์ตวันนี้เขียว
                    if execute.close_position(p).get("ok"):
                        log.info("🔔 ตลาดใกล้ปิด → ปิด %s (ไม้ $%.2f · พอร์ตวันนี้ $%.2f)",
                                 p.symbol, p.profit, day_pnl)
                        _state.pop(p.ticket, None)
            poss = m5.positions_get() or ()               # refetch หลังปิด

    alive = set()
    for p in poss:
        if p.magic != _MAGIC:
            continue
        alive.add(p.ticket)
        tick = m5.symbol_info_tick(p.symbol)
        if not tick:
            continue
        is_buy = p.type == m5.POSITION_TYPE_BUY
        cur = tick.bid if is_buy else tick.ask                  # ราคาที่ปิดไม้ได้จริง

        # 0) Take-profit ตาม % ราคา (ผู้ใช้กำหนด: ราคาขึ้นถึงเป้า → ปิดทั้งไม้ทันที)
        if tp_pct > 0:
            pchg = ((cur - p.price_open) if is_buy else (p.price_open - cur)) / p.price_open * 100
            if pchg >= tp_pct:
                execute.close_position(p)
                log.info("🎯 +%.2f%% ราคา (เป้า %.1f%%) → ปิด %s (#%s)", pchg, tp_pct, p.symbol, p.ticket)
                _state.pop(p.ticket, None)
                continue

        st = _state.setdefault(p.ticket, {"partial": False,
                                          "risk": abs(p.price_open - p.sl) if p.sl else None})
        if st["risk"] is None and p.sl:
            st["risk"] = abs(p.price_open - p.sl)
        R = st["risk"]
        if not R or R <= 0:          # ไม่รู้ระยะเสี่ยงเริ่มต้น → ข้าม (ปล่อย SL/TP ทำงาน)
            continue
        rmult = ((cur - p.price_open) if is_buy else (p.price_open - cur)) / R

        # 1) เพดาน R → ปิดทันที (ปิดไว้เมื่อใช้ price-% TP เพื่อให้ไม้วิ่งถึงเป้า %)
        if tp_pct <= 0 and rmult >= hard_r:
            execute.close_position(p)
            log.info("🎯 +%.2fR (เพดาน %.1fR) → ปิด %s (#%s)", rmult, hard_r, p.symbol, p.ticket)
            _state.pop(p.ticket, None)
            continue

        # 2) ถึง +PARTIAL_AT_R ครั้งแรก → เลื่อน SL เท่าทุน (+ ปิดบางส่วน เฉพาะโหมด R-based)
        if not st["partial"] and rmult >= partial_at:
            if tp_pct <= 0:                          # โหมด price-% TP ไม่ปิดบางส่วน (ปล่อยวิ่งถึงเป้า)
                half = _round_vol(p.volume * ratio, p.symbol)
                if 0 < half < p.volume:
                    if execute.close_position(p, half).get("ok"):
                        log.info("💰 ปิดบางส่วน %.2f lot ที่ +%.2fR %s", half, rmult, p.symbol)
            if execute.modify_sltp(p.ticket, p.price_open, p.tp):
                log.info("🛡️ เลื่อน SL เท่าทุน (+%.2fR) %s", rmult, p.symbol)
            st["partial"] = True
            continue

        # 3) trailing SL แบบ ATR-aware (เฉพาะ R-based · โหมด price-% TP ปล่อยไม้วิ่งถึงเป้า %)
        # dist = max(R-based, ATR-based) → ถ้าตลาดผันผวนเพิ่ม SL จะถ่างกว้างขึ้นตาม (ไม่โดน noise)
        if tp_pct <= 0 and st["partial"]:
            atr_now = _current_atr(p.symbol)
            dist = max(R * tfac, atr_now * tfac) if atr_now > 0 else R * tfac
            eps = cur * 1e-6
            if is_buy:
                new_sl = cur - dist
                if new_sl > (p.sl or 0) + eps:                  # ขยับขึ้นเท่านั้น (ล็อกกำไร)
                    execute.modify_sltp(p.ticket, new_sl, p.tp)
            else:
                new_sl = cur + dist
                if p.sl == 0 or new_sl < p.sl - eps:            # ขยับลงเท่านั้น
                    execute.modify_sltp(p.ticket, new_sl, p.tp)

    for t in list(_state):
        if t not in alive:
            _state.pop(t, None)
