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
    # ระบุ strategy type จาก ctx เพื่อปรับ prompt ให้ตรงกลุ่ม
    _src = ctx.get("source", "")
    _is_scalp = _src in ("ema_m5", "vwap", "bb_squeeze", "rsi_div", "orb_pro", "fx_orb", "scalp")
    _scalp_note = (
        "นี่คือ scalp trade (M5/M15) — ไม่ต้องการ trend ยาว แค่ momentum ระยะสั้น 30-90 นาที "
        "อย่าปฏิเสธเพราะ H1/D1 sideways — TF สั้นมีอิสระ "
    ) if _is_scalp else ""
    # Confluence — หลายกลยุทธ์ entry อิสระเห็นพ้องทิศเดียวกัน = หลักฐานยืนยันแข็งแรงขึ้น
    _confl = ctx.get("confluence") or []
    _confl_note = (
        f"⭐ ไม้นี้มี {len(_confl)} กลยุทธ์อิสระเห็นพ้อง ({', '.join(_confl)}) — "
        "confluence แบบนี้เพิ่มความน่าเชื่อถือของสัญญาณ ให้น้ำหนักบวก "
    ) if len(_confl) >= 2 else ""
    prompt = (
        "คุณเป็น prop firm risk manager ประเมินไม้เทรดนี้ **อย่างเป็นกลาง** "
        "ชั่งน้ำหนักทั้ง 2 ด้าน: โอกาสที่จะชนะ vs ความเสี่ยงที่จะแพ้ "
        f"{_scalp_note}{_confl_note}"
        "ห้ามทำนายราคา ห้ามแต่งข้อมูล ใช้ข้อมูลที่ให้มาเท่านั้น "
        "เกณฑ์: enter = สัญญาณดี risk/reward คุ้ม · small = สัญญาณพอใช้ risk สูงกว่าปกติ · "
        "skip = สัญญาณขัดแย้งหนัก หรือ R:R ไม่คุ้มเลย "
        "⚠️ ต้องตอบ enter หรือ small บ้าง — skip ทุกไม้คือ overcautious ไม่ใช่ risk management ที่ดี "
        "ถ้ามีความจำจากผลเทรดจริง ให้ใช้ประกอบ (ไม่ใช่กฎตายตัว). "
        "ตอบ JSON ตาม schema — decision, confidence 0-100, risks[], reason (ภาษาไทยสั้น):\n"
        + json.dumps(ctx, ensure_ascii=False) + mem
    )
    for model in (_MODEL, _FALLBACK):
        out = _call(prompt, key, model)
        if out is not None:
            out.setdefault("decision", "small")  # fallback default = small (ไม่บล็อก trade)
            return out
    # Gemini ใช้ไม่ได้ → ใช้ small (lot ครึ่ง) แทนที่จะ manual (บล็อกทุกไม้)
    log.warning("Gemini API ใช้ไม่ได้ทั้ง 2 model → fallback small (เปิดไม้ครึ่งขนาด)")
    return {"decision": "small", "confidence": 50, "risks": ["gemini_unavailable"],
            "reason": "Gemini ใช้ไม่ได้ — เปิดไม้ครึ่งขนาดอัตโนมัติ"}
