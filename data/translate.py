"""
data/translate.py — แปล + เรียบเรียงพาดหัวข่าวอังกฤษ → ไทยอ่านง่าย ด้วย Gemini (free tier)

ออกแบบให้ $0 + ไม่ชน limit:
- เรียกแบบ batch (ข่าวใหม่ทั้งรอบใน 1 request) → ประหยัด quota สุด
- cache ตาม news-ID ใน GCS (คำแปลไม่เปลี่ยน) → แทบไม่เรียกซ้ำ
- ไม่มี key / error / รูปแบบผิด → คืนต้นฉบับอังกฤษ (ระบบไม่พัง)

ต้องตั้ง env GEMINI_API_KEY (ฟรีจาก https://aistudio.google.com/apikey — ไม่ต้องผูกบัตร)
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

# gemini-2.5-flash: เร็ว ถูก แปลไทยดี — เปลี่ยนได้ด้วย env GEMINI_MODEL
# ถ้ารุ่นหลักโดนปลดระวาง (404) จะ fallback เป็น gemini-flash-latest (alias ชี้รุ่นล่าสุดเสมอ)
_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
_FALLBACK_MODEL = "gemini-flash-latest"
_CACHE_FILE = "translate_cache.json"
_KEEP_DAYS = 30  # คำแปลเก่ากว่านี้ prune ทิ้ง (กันไฟล์บวม)


def _key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def _cache_load() -> dict:
    try:
        from watchlist import store
        return store.load_json(_CACHE_FILE, {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _cache_save(c: dict) -> None:
    try:
        from watchlist import store
        store.save_json(_CACHE_FILE, c)
    except Exception as e:  # noqa: BLE001
        log.warning("translate cache save failed: %s", e)


def _gemini_translate(texts: list[str]) -> Optional[list[str]]:
    """แปล batch ด้วย Gemini — ลองรุ่นหลักก่อน, ถ้าพลาด (เช่น 404 ปลดระวาง) ลอง flash-latest
    คืน list ไทย (เรียงตรง input) หรือ None ถ้าทุกรุ่นพลาด"""
    if not texts:
        return []
    for model in (_MODEL, _FALLBACK_MODEL):
        out = _gemini_call(texts, model)
        if out is not None:
            return out
    return None


def _gemini_call(texts: list[str], model: str) -> Optional[list[str]]:
    """เรียก Gemini รุ่นเดียว → list ไทย หรือ None ถ้าพลาด"""
    try:
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        prompt = (
            "แปลและเรียบเรียงพาดหัวข่าวหุ้น/การเงินต่อไปนี้เป็นภาษาไทยที่สั้น กระชับ "
            "เป็นธรรมชาติ อ่านเข้าใจง่ายสำหรับนักลงทุนทั่วไป "
            "คงชื่อบริษัทและตัวย่อหุ้น (เช่น AAPL, Tesla, Nvidia) ไว้เป็นภาษาอังกฤษ "
            "ห้ามเพิ่มความเห็น/ข้อมูลที่ไม่มีในต้นฉบับ และห้ามใส่เครื่องหมายคำพูด "
            "ตอบกลับเป็น JSON array ของสตริงไทยเท่านั้น เรียงลำดับให้ตรงกับต้นฉบับ:\n"
            + json.dumps(texts, ensure_ascii=False)
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json",
                "response_schema": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }
        r = requests.post(url, params={"key": _key()}, json=body, timeout=20)
        if r.status_code != 200:
            log.warning("gemini translate (%s) → %s %s", model, r.status_code, r.text[:120])
            return None
        data = r.json()
        txt = data["candidates"][0]["content"]["parts"][0]["text"]
        out = json.loads(txt)
        if isinstance(out, list) and len(out) == len(texts):
            return [str(x) for x in out]
        log.warning("gemini translate: จำนวนผลลัพธ์ไม่ตรง input")
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("gemini translate failed: %s", e)
        return None


def _google_free(text: str) -> Optional[str]:
    """Google Translate ฟรี (ไม่ต้อง key) — แปลตรงตัว ทีละพาดหัว
    endpoint สาธารณะ gtx; คืน None ถ้าพลาด → ระบบจะคงอังกฤษไว้"""
    try:
        import requests
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "en", "tl": "th", "dt": "t", "q": text},
            timeout=12,
        )
        if r.status_code != 200:
            log.warning("google translate free → %s", r.status_code)
            return None
        data = r.json()
        # data[0] = [[แปล, ต้นฉบับ, ...], ...] → ต่อทุก segment
        return "".join(seg[0] for seg in data[0] if seg and seg[0]) or None
    except Exception as e:  # noqa: BLE001
        log.warning("google translate free failed: %s", e)
        return None


def _translate_todo(texts: list[str]) -> Optional[list[str]]:
    """แปลรายการที่ยังไม่เคยแปล: Gemini ก่อน (ถ้ามี key ใช้ได้) → ตก Google ฟรี
    คืน list ไทย (เรียงตรง input) หรือ None ถ้าทุกตัวแปลไม่ได้เลย"""
    if not texts:
        return []
    if enabled():
        g = _gemini_translate(texts)  # batch — แปล+เรียบเรียงสวยสุด
        if g:
            return g
        log.info("Gemini ใช้ไม่ได้ → fallback Google Translate ฟรี")
    out: list[str] = []
    any_ok = False
    for t in texts:
        g = _google_free(t)
        out.append(g if g else t)
        any_ok = any_ok or bool(g)
    return out if any_ok else None


def to_thai(items: list[dict]) -> dict:
    """รับ list[{'id','headline'}] → คืน {id: ไทย}
    - cache hit ไม่เรียก API; เฉพาะที่ยังไม่เคยแปล → แปล (Gemini→Google ฟรี)
    - แปลพลาดทั้งหมด → คืน headline อังกฤษเดิม (ไม่พัง, ไม่ cache เพื่อให้ลองใหม่รอบหน้า)"""
    result: dict = {}
    if not items:
        return result
    cache = _cache_load()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    todo_ids: list[str] = []
    todo_texts: list[str] = []
    for it in items:
        nid = str(it.get("id"))
        hl = it.get("headline") or ""
        ent = cache.get(nid)
        if ent and ent.get("th"):
            result[nid] = ent["th"]
        else:
            result[nid] = hl  # fallback ชั่วคราว
            if hl:
                todo_ids.append(nid)
                todo_texts.append(hl)

    if todo_texts:
        th = _translate_todo(todo_texts)
        if th:
            changed = False
            for nid, t, src in zip(todo_ids, th, todo_texts):
                t = (t or "").strip() or src
                result[nid] = t
                if t != src:  # cache เฉพาะที่แปลได้จริง (กันล็อกอังกฤษไว้ถาวร)
                    cache[nid] = {"th": t, "ts": now}
                    changed = True
            if changed:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=_KEEP_DAYS)).isoformat(timespec="seconds")
                cache = {k: v for k, v in cache.items() if (v.get("ts") or "") >= cutoff}
                _cache_save(cache)
    return result
