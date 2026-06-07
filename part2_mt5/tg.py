"""
part2_mt5/tg.py — Telegram แบบโต้ตอบ (ปุ่มกด) สำหรับบอท Part 2

ใช้ getUpdates (long-poll) รับการกดปุ่ม → ต้องเป็น "บอทตัวที่ 2" แยกจาก Part 1
(บอท Part 1 ใช้ webhook อยู่ — ตัวเดียวกันใช้ getUpdates ไม่ได้)
"""
from __future__ import annotations
import logging

log = logging.getLogger("part2.tg")
_API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, timeout: int = 35, **params):
    import requests
    try:
        r = requests.post(_API.format(token=token, method=method), json=params, timeout=timeout)
        j = r.json()
        if not j.get("ok"):
            log.warning("tg %s → %s", method, j.get("description"))
        return j
    except Exception as e:  # noqa: BLE001
        log.warning("tg %s failed: %s", method, str(e).replace(token, "***"))  # กัน token หลุดลง log
        return None


def send_ticket(token: str, chat_id: str, text: str, ticket_id: str) -> "int | None":
    """ส่งใบสั่ง + ปุ่ม ✅ เปิดออเดอร์ / ❌ ไม่เปิด → คืน message_id"""
    kb = {"inline_keyboard": [[
        {"text": "✅ เปิดออเดอร์", "callback_data": f"open:{ticket_id}"},
        {"text": "❌ ไม่เปิด", "callback_data": f"skip:{ticket_id}"},
    ]]}
    j = _call(token, "sendMessage", timeout=20, chat_id=chat_id, text=text,
              reply_markup=kb, disable_web_page_preview=True)
    return j["result"]["message_id"] if j and j.get("ok") else None


def get_updates(token: str, offset: int) -> list:
    """ดึง updates (ปุ่มกด callback_query + ข้อความคำสั่ง message) ตั้งแต่ offset"""
    j = _call(token, "getUpdates", timeout=15, offset=offset,
              allowed_updates=["callback_query", "message"])
    return j.get("result", []) if j and j.get("ok") else []


def send_text(token: str, chat_id: str, text: str) -> None:
    """ส่งข้อความธรรมดา (ตอบคำสั่ง /status /stats)"""
    _call(token, "sendMessage", timeout=15, chat_id=chat_id, text=text, disable_web_page_preview=True)


def set_commands(token: str) -> None:
    """ลงทะเบียนเมนูคำสั่ง (โผล่ตอนพิมพ์ / ใน Telegram) — เรียกครั้งเดียวตอนเริ่ม"""
    cmds = [
        {"command": "status", "description": "สถานะ · โหมด · พอร์ต · P/L วันนี้ · ไม้ที่เปิด"},
        {"command": "stats", "description": "สถิติผลเทรด (win rate · profit factor)"},
        {"command": "insights", "description": "บทเรียน: เทคนิคไหนได้เงินจริง (บอทเรียนรู้)"},
        {"command": "pause", "description": "หยุดเปิดไม้ใหม่ชั่วคราว (auto)"},
        {"command": "resume", "description": "กลับมาเปิดไม้อัตโนมัติ"},
        {"command": "closeall", "description": "ปิดไม้ Part 2 ทั้งหมดทันที (ฉุกเฉิน)"},
        {"command": "update",   "description": "ดึงโค้ดใหม่จาก GitHub แล้ว restart อัตโนมัติ"},
        {"command": "stop",     "description": "หยุดบอท (ไม้เปิดอยู่ยังคงเปิดใน MT5)"},
        {"command": "restart",  "description": "Restart บอท (ไม่ดึงโค้ดใหม่)"},
        {"command": "help", "description": "รายการคำสั่งทั้งหมด"},
    ]
    _call(token, "setMyCommands", timeout=10, commands=cmds)


def ack_updates(token: str, offset: int) -> None:
    """ยืนยัน offset กับ Telegram server ก่อน exit — กัน bot อ่าน command เก่าซ้ำหลัง restart
    Telegram marks updates < offset ว่า consumed ทันทีที่รับ request นี้"""
    _call(token, "getUpdates", timeout=5, offset=offset, limit=1)


def answer_callback(token: str, cb_id: str, text: str = "") -> None:
    _call(token, "answerCallbackQuery", timeout=10, callback_query_id=cb_id, text=text)


def edit_text(token: str, chat_id: str, message_id: int, text: str) -> None:
    _call(token, "editMessageText", timeout=10, chat_id=chat_id, message_id=message_id, text=text)


def delete_msg(token: str, chat_id: str, message_id: int) -> None:
    _call(token, "deleteMessage", timeout=10, chat_id=chat_id, message_id=message_id)
