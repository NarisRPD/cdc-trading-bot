"""
runtime_flags.py — สวิตช์รันไทม์ที่ทั้ง 3 service เห็นตรงกัน (เก็บใน GCS เดียวกับ watchlist)

ตอนนี้มีตัวเดียว: alerts_paused (โหมด /pause) — เงียบแจ้งเตือนอัตโนมัติทั้งหมด
(สแกน 07:00 · โซนเปลี่ยน · ข่าว · บรีฟเช้า · รีวิวสัปดาห์) โดยไม่แตะคำสั่ง on-demand

bot (Service) เขียน flag ผ่าน /pause /resume · scanner/watchlist (Jobs) อ่านตอนจะส่ง Telegram
best-effort: อ่านไม่ได้ = ถือว่า "ไม่ปิด" (fail-open — ยอมส่งดีกว่าเงียบหายโดยไม่ตั้งใจ)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_FILE = "bot_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status() -> dict:
    """คืน state ทั้งก้อน (dict ว่างถ้าไม่มี/อ่านไม่ได้)"""
    try:
        from watchlist import store
        st = store.load_json(_FILE, {})
        return st if isinstance(st, dict) else {}
    except Exception as e:  # noqa: BLE001
        log.warning("runtime_flags.status อ่านไม่ได้: %s", e)
        return {}


def is_paused() -> bool:
    """แจ้งเตือนอัตโนมัติถูกพักอยู่ไหม (fail-open = False ถ้าอ่านไม่ได้)"""
    return bool(status().get("alerts_paused", False))


def set_paused(paused: bool, by: str = "") -> dict:
    """ตั้ง/ยกเลิกโหมดพักแจ้งเตือน — คืน state ใหม่ (raise ถ้าเขียนไม่ได้ ให้ caller รู้)"""
    from watchlist import store
    st = store.load_json(_FILE, {})
    if not isinstance(st, dict):
        st = {}
    st["alerts_paused"] = bool(paused)
    st["updated_at"] = _now_iso()
    if by:
        st["updated_by"] = by
    store.save_json(_FILE, st)
    return st
