"""
part2_mt5/journal.py — บันทึกผลเทรดของ Part 2 + สถิติ + P/L วันนี้

อ่านจาก history ของ MT5 (เฉพาะดีล magic 260605) → บันทึกไม้ที่ปิดแล้วลง part2_journal.json
ใช้คำนวณ win rate / profit factor (รู้ว่าระบบทำเงินจริงไหม) + เช็กขาดทุนต่อวัน
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, time as dtime, timedelta

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
    """อ่านดีลปิด (DEAL_ENTRY_OUT) magic Part 2 ย้อนหลัง → บันทึกอันใหม่ คืน list ไม้ที่เพิ่งปิด (ใช้รายงาน Telegram)"""
    import MetaTrader5 as m5
    j = _load()
    seen = {t["deal_id"] for t in j}
    deals = m5.history_deals_get(datetime.now() - timedelta(days=days_back), datetime.now() + timedelta(minutes=1))
    if not deals:
        return []
    new = []
    for d in deals:
        if d.magic != _MAGIC or d.entry != m5.DEAL_ENTRY_OUT or d.ticket in seen:
            continue
        entry = {"deal_id": d.ticket, "symbol": d.symbol, "time": d.time,
                 "profit": round(d.profit + d.commission + d.swap, 2), "volume": d.volume,
                 "position_id": d.position_id}   # ใช้ lookup ชื่อเทคนิคจาก part2_trade_meta.json
        j.append(entry)
        new.append(entry)
    if new:
        _save(j)
        log.info("journal: บันทึกไม้ปิดใหม่ %d", len(new))
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
    start = datetime.combine(datetime.now().date(), dtime.min)
    deals = m5.history_deals_get(start, datetime.now() + timedelta(minutes=1)) or []
    realized = sum(d.profit + d.commission + d.swap for d in deals
                   if d.magic == _MAGIC and d.entry == m5.DEAL_ENTRY_OUT)
    floating = sum(p.profit for p in (m5.positions_get() or []) if p.magic == _MAGIC)
    return round(realized + floating, 2)
