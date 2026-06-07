"""
notify/telegram.py — chunked sender (กัน 4096 char limit)
- ส่งล้มเหลวก็ไม่ raise ขึ้นไป crash ทั้งระบบ (log + คืน False)
- split โดยรักษา line boundary ก่อน ไม่ตัดกลาง symbol
"""
from __future__ import annotations
import logging
import time
from typing import Iterable, List

import requests

log = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_LEN = 4000  # ต่ำกว่า 4096 เผื่อ overhead (markdown / emoji multi-byte)


def _split_by_line(text: str, max_len: int) -> List[str]:
    """fallback: แบ่งตามบรรทัด (ใช้เมื่อบล็อกเดียวยาวเกิน max_len)"""
    chunks: List[str] = []
    buf: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if cur_len + line_len > max_len and buf:
            chunks.append("\n".join(buf))
            buf = []
            cur_len = 0
        if line_len > max_len:
            for i in range(0, len(line), max_len):
                chunks.append(line[i : i + max_len])
            continue
        buf.append(line)
        cur_len += line_len
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _split_message(text: str, max_len: int = _MAX_LEN) -> List[str]:
    """
    แบ่งข้อความยาวเป็นหลายชิ้น โดยตัดที่ "ขอบบล็อก" (บรรทัดว่างคั่นแต่ละสัญญาณ)
    → ไม่หั่นบล็อกสัญญาณกลางคัน (เช่น ⏱️/เป้าราคา ไม่หลุดไปคนละข้อความ)
    บล็อกเดียวที่ยาวเกินจริง ๆ ค่อย fallback ตัดตามบรรทัด
    """
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    buf: list[str] = []
    cur_len = 0
    for block in text.split("\n\n"):  # บล็อก = คั่นด้วยบรรทัดว่าง
        block_len = len(block) + 2  # +2 = '\n\n'
        if cur_len + block_len > max_len and buf:
            chunks.append("\n\n".join(buf))
            buf = []
            cur_len = 0
        if len(block) > max_len:
            # บล็อกเดียวยาวเกิน (แทบไม่เกิด) → ตัดตามบรรทัด
            chunks.extend(_split_by_line(block, max_len))
            continue
        buf.append(block)
        cur_len += block_len

    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def send_telegram(
    message: str,
    *,
    token: str,
    chat_id: str,
    timeout: int = 30,
    parse_mode: str | None = None,
) -> bool:
    """
    ส่งข้อความ (อาจถูก split เป็นหลาย message)
    คืน True ถ้าส่งครบทุกชิ้น, False ถ้ามีชิ้นไหนเฟล
    """
    if not message.strip():
        return True

    parts = _split_message(message)
    url = _API.format(token=token)
    ok_all = True

    for i, part in enumerate(parts, 1):
        payload = {
            "chat_id": chat_id,
            "text": part,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        for attempt in range(1, 4):
            try:
                r = requests.post(url, data=payload, timeout=timeout)
                if r.status_code == 200:
                    log.info("Telegram sent (part %d/%d, %d chars)",
                             i, len(parts), len(part))
                    break
                # Telegram rate limit → respect retry_after
                if r.status_code == 429:
                    try:
                        retry_after = r.json().get("parameters", {}).get("retry_after", 2)
                    except Exception:  # noqa: BLE001
                        retry_after = 2
                    log.warning("Telegram 429 — wait %ss", retry_after)
                    time.sleep(retry_after + 0.5)
                    continue
                log.error("Telegram error %s: %s", r.status_code, r.text[:200])
                ok_all = False
                break
            except requests.RequestException as e:
                log.warning("Telegram send attempt %d failed: %s", attempt, e)
                if attempt == 3:
                    ok_all = False
                else:
                    time.sleep(1.5 * attempt)

        # กัน rate limit ระหว่าง part
        if i < len(parts):
            time.sleep(0.5)

    return ok_all


def send_many(
    messages: Iterable[str],
    *,
    token: str,
    chat_id: str,
    timeout: int = 30,
) -> int:
    """ส่งหลายข้อความ — คืนจำนวนที่สำเร็จ"""
    ok = 0
    for m in messages:
        if send_telegram(m, token=token, chat_id=chat_id, timeout=timeout):
            ok += 1
    return ok
