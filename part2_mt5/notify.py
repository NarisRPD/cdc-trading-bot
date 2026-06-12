"""
part2_mt5/notify.py — ส่งข้อความเข้า Telegram (แชตเดิม) สำหรับ Scalping Bot
ใช้บอทตัวที่ 2 หรือบอทเดิมก็ได้ (ตั้งใน config.env)
"""
from __future__ import annotations
import logging

log = logging.getLogger("part2.notify")


def send(text: str, token: str, chat_id: str, timeout: int = 15) -> bool:
    if not token or not chat_id:
        log.warning("ไม่มี TELEGRAM token/chat_id — ข้ามส่ง")
        return False
    import requests
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=timeout,
        )
        if r.status_code != 200:
            log.warning("telegram → %s %s", r.status_code, r.text[:120])
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("telegram send failed: %s", e)
        return False
