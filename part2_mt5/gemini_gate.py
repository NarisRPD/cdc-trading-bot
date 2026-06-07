"""
part2_mt5/gemini_gate.py — Gemini "ด่านปิดช่องโหว่" ก่อนออกใบสั่งเทรด

หลักการ (กัน hallucination): ป้อน "ภาพรวมไม้จริง" (ตัวเลขจาก signal + แท่งเทียน + risk)
ให้ Gemini ทำหน้าที่ "ทนายฝ่ายตรงข้าม" — หาเหตุผลว่าไม้นี้จะแพ้ได้ยังไง
→ จับความผิดพลาดที่เห็นชัดแต่คนมองข้ามตอนใจร้อน (ไล่ของแพง/สวนเทรนด์/R:R แย่/ชนแนวต้าน)

*** ไม่ทำนายราคา · ไม่การันตีกำไร · แค่ลด "ขาดทุนโง่ ๆ ที่เลี่ยงได้" ***
ตัดสิน enter / small / skip — แต่คนยังกดเองเสมอ
"""
from __future__ import annotations
import json
import logging
import os
from typing import Optional

log = logging.getLogger("part2.gemini_gate")

_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
_FALLBACK = "gemini-flash-latest"

_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "decision": {"type": "STRING", "enum": ["enter", "small", "skip"]},
        "confidence": {"type": "INTEGER"},  # 0-100
        "risks": {"type": "ARRAY", "items": {"type": "STRING"}},
        "reason": {"type": "STRING"},
    },
    "required": ["decision", "confidence", "risks", "reason"],
}


def _call(prompt: str, key: str, model: str) -> Optional[dict]:
    try:
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json",
                                 "response_schema": _SCHEMA},
        }
        r = requests.post(url, params={"key": key}, json=body, timeout=45)
        if r.status_code != 200:
            log.warning("gemini_gate (%s) → %s %s", model, r.status_code, r.text[:120])
            return None
        return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:  # noqa: BLE001
        log.warning("gemini_gate failed: %s", e)
        return None


def assess(ctx: dict, api_key: Optional[str] = None, memory: str = "") -> dict:
    """ctx = ภาพรวมไม้จริง (symbol, direction, entry, sl, tp, rr, lot, source, st_value,
    candles, volume, position_in_trend, near_resistance, news ...)
    memory = บทเรียนจากสถิติจริงที่ผ่านมา (ถ้ามี) → AI ใช้เป็นน้ำหนักประกอบให้ฉลาดขึ้น
    คืน {decision, confidence, risks[], reason} · ไม่มี key → ให้คนตรวจเอง (ไม่บล็อก)"""
    key = api_key or os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return {"decision": "manual", "confidence": None, "risks": [],
                "reason": "ไม่มี Gemini key — ให้ผู้เทรดตรวจเอง"}
    mem = ("\n\nความจำจากผลเทรดจริงที่ผ่านมา (ใช้ปรับน้ำหนัก ไม่ใช่กฎตายตัว): " + memory) if memory else ""
    prompt = (
        "คุณเป็น risk manager มืออาชีพ ตรวจ 'ก่อนเข้าไม้' โดยทำหน้าที่ทนายฝ่ายตรงข้าม "
        "หาเหตุผลว่าไม้นี้อาจแพ้/ไม่ควรเข้า จากข้อมูลจริงเท่านั้น (ห้ามทำนายราคา ห้ามแต่งข้อมูล) "
        "พิจารณา: ยืดเกิน/ไล่ของแพง, สวนเทรนด์ใหญ่ (Stage), R:R ไม่คุ้ม, เทรนด์ขรุขระ (R² ต่ำ), "
        "วอลุ่มไม่ยืนยัน, ชนแนวต้าน/รับ, สัญญาณแท่งเทียนขัดทิศ. "
        "ถ้ามีความจำจากผลเทรดจริง ให้ใช้เป็นน้ำหนักประกอบการตัดสิน (แต่ไม่ใช่กฎตายตัว เพราะตลาดเปลี่ยนได้). "
        "ตัดสิน decision: enter (เข้าได้) / small (เข้าไม้เล็ก ความเสี่ยงสูง) / skip (ข้าม) "
        "พร้อม confidence 0-100, risks (ลิสต์ความเสี่ยงที่เจอ), reason (เหตุผลสั้นไทย). "
        "ตอบ JSON ตาม schema:\n" + json.dumps(ctx, ensure_ascii=False) + mem
    )
    for model in (_MODEL, _FALLBACK):
        out = _call(prompt, key, model)
        if out is not None:
            out.setdefault("decision", "manual")
            return out
    return {"decision": "manual", "confidence": None, "risks": [],
            "reason": "Gemini ใช้ไม่ได้ตอนนี้ — ให้ผู้เทรดตรวจเอง"}
