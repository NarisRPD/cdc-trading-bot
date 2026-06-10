"""
part2_mt5/journal.py — บันทึกผลเทรดของ Part 2 + สถิติ + P/L วันนี้

อ่านจาก history ของ MT5 (เฉพาะดีล magic 260605) → บันทึกไม้ที่ปิดแล้วลง part2_journal.json
ใช้คำนวณ win rate / profit factor (รู้ว่าระบบทำเงินจริงไหม) + เช็กขาดทุนต่อวัน
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, time as dtime, timedelta, timezone

log = logging.getLogger("part2.journal")
_FILE = os.path.join(os.path.dirname(__file__), "part2_journal.json")
_MAGIC = 260605


def _load() -> list:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or []
    except Exception:  # noqa: BLE001
        pass
    return []


def _save(data: list) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log.warning("save journal failed: %s", e)


def record_closed(days_back: int = 7) -> list:
    """อ่านดีลปิด (DEAL_ENTRY_OUT) magic Part 2 ย้อนหลัง → บันทึกอันใหม่ คืน list ไม้ที่เพิ่งปิด (ใช้รายงาน Telegram)

    แต่ละ entry มี: deal_id · symbol · time · profit · volume · position_id · direction · close_reason
      direction    = "buy" / "sell" (ทิศทางของ position ที่ถูกปิด)
      close_reason = "tp" / "sl" / "bot" / "manual"
    """
    import MetaTrader5 as m5
    j = _load()
    seen = {t["deal_id"] for t in j}
    # UTC-aware — MT5 Python รับ timezone-aware datetime ได้ และแปลงเป็น UTC ให้เอง
    # ถ้าใช้ datetime.now() (naive, local VPS time) บน VPS ที่ timezone ≠ UTC จะเพี้ยน
    _now = datetime.now(timezone.utc)
    deals = m5.history_deals_get(_now - timedelta(days=days_back), _now + timedelta(minutes=1))
    if not deals:
        return []

    # โหลด order history ครั้งเดียว → dict {ticket: order} — ใช้เป็น fallback
    # เมื่อ deal.reason อ่านไม่ได้เท่านั้น (ปกติอ่านจาก deal โดยตรง)
    _ord_map: dict = {}
    try:
        _all_ords = m5.history_orders_get(
            _now - timedelta(days=days_back), _now + timedelta(minutes=1)
        ) or []
        _ord_map = {o.ticket: o for o in _all_ords}
    except Exception:   # noqa: BLE001
        pass            # ไม่มี order history → fallback ใช้ไม่ได้ (close_reason อาจว่าง)

    new = []
    for d in deals:
        if d.magic != _MAGIC or d.entry != m5.DEAL_ENTRY_OUT or d.ticket in seen:
            continue

        # ทิศทาง position ที่ปิด:
        #   DEAL_TYPE_SELL (1) = ขายเพื่อปิด → position เดิมเป็น Buy
        #   DEAL_TYPE_BUY  (0) = ซื้อเพื่อปิด → position เดิมเป็น Sell
        direction = "buy" if d.type == m5.DEAL_TYPE_SELL else "sell"

        # เหตุผลปิด: อ่านจาก deal.reason โดยตรง (แม่นสุด — ติดมากับ deal ไม่ต้อง lookup)
        # DEAL_REASON_*: 0=client(มือ/desktop) 1=mobile 2=web 3=expert(บอท) 4=SL 5=TP 6=stop-out
        # *** ห้าม default เป็น "manual" — เคยติดป้าย Manual ทั้งที่จริงชน SL (lookup order พลาด)
        #     ทำให้ผู้ใช้สับสนว่าใครปิด · ไม่รู้จริง → เว้นว่างดีกว่าเดาผิด ***
        _REASON_MAP = {5: "tp", 4: "sl", 3: "bot", 0: "manual", 1: "manual", 2: "manual", 6: "so"}
        close_reason = _REASON_MAP.get(getattr(d, "reason", None), "")
        if not close_reason:
            # fallback: deal.reason ใช้ไม่ได้ (โบรก/เวอร์ชันเก่า) → ดูจาก order ที่ trigger deal
            _o = _ord_map.get(d.order)
            if _o is not None:
                close_reason = _REASON_MAP.get(getattr(_o, "reason", None), "")

        entry = {"deal_id": d.ticket, "symbol": d.symbol, "time": d.time,
                 "profit": round(d.profit + d.commission + d.swap, 2), "volume": d.volume,
                 "position_id": d.position_id,            # ใช้ lookup ชื่อเทคนิคจาก part2_trade_meta.json
                 "direction": direction, "close_reason": close_reason}
        j.append(entry)
        new.append(entry)
    if new:
        _save(j)
        log.info("journal: บันทึกไม้ปิดใหม่ %d รายการ", len(new))
    return new


def compute_stats() -> "dict | None":
    j = _load()
    if not j:
        return None
    pnls = [t["profit"] for t in j]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_win, gross_loss = sum(wins), abs(sum(losses))
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else 0,
        "total": round(sum(pnls), 2),
        "avg": round(sum(pnls) / n, 2) if n else 0,
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "best": round(max(pnls), 2), "worst": round(min(pnls), 2),
    }


def today_pnl() -> float:
    """กำไร/ขาดทุนวันนี้ = realized (ดีลปิดวันนี้) + floating (position เปิด) ของ Part 2"""
    import MetaTrader5 as m5
    # UTC เที่ยงคืน = จุดเริ่มวันของ MT5 server (ส่วนใหญ่ UTC หรือ UTC+2/+3)
    # ใช้ UTC-aware เพื่อให้ถูกต้องบน VPS ทุก timezone (ไม่พึ่ง OS local time)
    _now = datetime.now(timezone.utc)
    start = _now.replace(hour=0, minute=0, second=0, microsecond=0)  # เที่ยงคืน UTC วันนี้
    deals = m5.history_deals_get(start, _now + timedelta(minutes=1)) or []
    realized = sum(d.profit + d.commission + d.swap for d in deals
                   if d.magic == _MAGIC and d.entry == m5.DEAL_ENTRY_OUT)
    floating = sum(p.profit for p in (m5.positions_get() or []) if p.magic == _MAGIC)
    return round(realized + floating, 2)
