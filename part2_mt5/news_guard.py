"""
part2_mt5/news_guard.py — เลี่ยงเปิดไม้ช่วงข่าวแรง (NFP/FOMC/CPI)

ดึงปฏิทินเศรษฐกิจ US high-impact จาก Finnhub (cache 30 นาที กัน rate limit)
ถ้ามีข่าวแรงภายใน BLACKOUT_MIN นาที (ก่อน/หลัง) → งดเปิดไม้ใหม่ (gap เสี่ยง)
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("part2.news_guard")
_cache: dict = {"at": None, "events": []}


def _fetch(api_key: str) -> list:
    """คืน list ของ (datetime_utc, label) ของ US high-impact วันนี้-พรุ่งนี้"""
    import requests
    out = []
    try:
        now = datetime.now(timezone.utc)
        r = requests.get("https://finnhub.io/api/v1/calendar/economic",
                         params={"token": api_key,
                                 "from": now.strftime("%Y-%m-%d"),
                                 "to": (now + timedelta(days=1)).strftime("%Y-%m-%d")},
                         timeout=12)
        if r.status_code != 200:
            return []
        for e in (r.json().get("economicCalendar") or []):
            if (e.get("country") or "").upper() not in ("US", "USA"):
                continue
            imp = str(e.get("impact", "")).lower()
            if imp not in ("3", "high"):     # เอาเฉพาะ high-impact
                continue
            t = e.get("time") or ""
            try:
                dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                out.append((dt, e.get("event", "ข่าว US")))
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        log.warning("ดึงปฏิทินข่าวไม่สำเร็จ: %s", e)
    return out


def is_blackout(api_key: str, within_min: int = 30) -> "tuple[bool, str]":
    """คืน (True, ชื่อข่าว) ถ้ามีข่าวแรงภายใน within_min นาที · (False,'') ถ้าโล่ง"""
    if not api_key:
        return (False, "")
    now = datetime.now(timezone.utc)
    if _cache["at"] is None or (now - _cache["at"]).total_seconds() > 1800:  # refetch ทุก 30 นาที
        _cache["events"] = _fetch(api_key)
        _cache["at"] = now
    win = timedelta(minutes=within_min)
    for dt, label in _cache["events"]:
        if abs((dt - now).total_seconds()) <= win.total_seconds():
            return (True, label)
    return (False, "")
