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
                # รองรับทั้ง "YYYY-MM-DD HH:MM:SS" (intraday) และ "YYYY-MM-DD" (all-day events)
                # all-day events เช่น Fed meeting day ถ้าข้ามไปจะพลาดช่วง blackout สำคัญ
                try:
                    dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    dt = datetime.strptime(t, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                out.append((dt, e.get("event", "ข่าว US")))
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        log.warning("ดึงปฏิทินข่าวไม่สำเร็จ: %s", e)
    return out


def _is_crypto_thin_hours() -> "tuple[bool, str]":
    """ช่วงวอลุ่มบางสำหรับ crypto (เสาร์เช้า UTC · อาทิตย์กลางคืน UTC)
    ช่วงเหล่านี้ราคาเด้งง่าย SL โดนได้จาก noise ล้วนๆ — ไม่มี institutional flow"""
    now = datetime.now(timezone.utc)
    wd, h = now.weekday(), now.hour   # Mon=0 .. Sat=5, Sun=6
    if wd == 5 and 0 <= h < 5:       # เสาร์ 00:00-05:00 UTC — บางมาก (ก่อน Asia open)
        return True, f"crypto ตลาดบาง เสาร์ {h:02d}:00 UTC"
    if wd == 6 and 21 <= h <= 23:    # อาทิตย์ 21:00-23:59 UTC — ก่อน London/NY ขึ้นมา
        return True, f"crypto ตลาดบาง อาทิตย์ {h:02d}:00 UTC"
    return False, ""


def _is_crypto_options_expiry() -> "tuple[bool, str]":
    """Options expiry crypto (Deribit/OKX) — ทุกศุกร์สุดท้ายของเดือน 08:00 UTC ±2ชม
    ช่วงนี้ราคามักโดน pin ที่ strike price แล้วพุ่งหลังหมดอายุ — อย่าเข้าตอนนี้"""
    import calendar as _cal
    now = datetime.now(timezone.utc)
    if now.weekday() != 4:   # ไม่ใช่ศุกร์
        return False, ""
    _, last_day = _cal.monthrange(now.year, now.month)
    last_fri = max(d for d in range(1, last_day + 1)
                   if datetime(now.year, now.month, d, tzinfo=timezone.utc).weekday() == 4)
    if now.day == last_fri and 6 <= now.hour <= 10:   # ±2ชม รอบ 08:00 UTC
        return True, f"Crypto Options Expiry ศุกร์สุดท้าย {now.strftime('%d/%m')} ~08:00 UTC"
    return False, ""


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


def is_blackout_crypto(api_key: str, within_min: int = 30) -> "tuple[bool, str]":
    """blackout สำหรับ crypto โดยเฉพาะ (3 ชั้น):
      1) ข่าว US high-impact (FOMC/CPI/NFP — กระทบทุกสินทรัพย์รวม crypto)
      2) ช่วงวอลุ่มบาง (เสาร์เช้า / อาทิตย์กลางคืน UTC) — noise สูง
      3) Crypto options expiry (ศุกร์สุดท้ายของเดือน ~08:00 UTC) — ราคาถูก pin"""
    blk, label = is_blackout(api_key, within_min)
    if blk:
        return blk, label
    thin, reason = _is_crypto_thin_hours()
    if thin:
        return True, reason
    exp, reason = _is_crypto_options_expiry()
    if exp:
        return True, reason
    return False, ""
