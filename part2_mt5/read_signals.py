"""
part2_mt5/read_signals.py — ดึงสัญญาณ CDC จาก Part 1 ผ่าน HTTPS (/signals)

Part 1 (Cloud) ปล่อยสัญญาณลง GCS → cdc-bot เสิร์ฟที่ /signals → Scalping Bot ดึงมาใช้
ไม่ต้องมี GCP key บนเครื่อง Scalping Bot (ใช้แค่ token)
"""
from __future__ import annotations
import logging

log = logging.getLogger("part2.read_signals")


def fetch_signals(bot_url: str, token: str, timeout: int = 15) -> list[dict]:
    """ดึงสัญญาณล่าสุดจาก Part 1 — คืน list[dict] (ว่างถ้าพลาด)"""
    import requests
    try:
        r = requests.get(bot_url.rstrip("/") + "/signals", params={"key": token}, timeout=timeout)
        if r.status_code != 200:
            log.warning("fetch signals → %s %s", r.status_code, r.text[:120])
            return []
        return r.json().get("signals", []) or []
    except Exception as e:  # noqa: BLE001
        log.warning("fetch signals failed: %s", e)
        return []


def tradable_for_mt5(signals: list[dict], allowed_markets: tuple[str, ...]) -> list[dict]:
    """กรองเฉพาะตลาดที่ตั้งใจเทรดผ่าน MT5 (ตาม config ALLOWED_MARKETS)
    หมายเหตุ: ตลาด us = ต้องเป็นเมกะแคปที่โบรกมี (เช็ก symbol map อีกชั้นตอน execute)"""
    allowed = set(allowed_markets)
    return [s for s in signals if s.get("market") in allowed]
