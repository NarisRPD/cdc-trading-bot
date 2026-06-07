"""
data/ai.py — ตัวกลางเรียก Gemini สำหรับงาน "วิเคราะห์/สรุป/สังเคราะห์"

หลักการ (กัน hallucination ในเรื่องเงิน):
- ป้อน "ข้อมูลจริงที่บอทมีอยู่แล้ว" (ข่าว/งบ/โซน/Stage/พอร์ต) ให้ Gemini เรียบเรียง
- ห้ามให้แต่งข้อมูล/ทำนายราคา · ทุกคำตอบมี disclaimer ฝั่ง caller
- บอทยังเป็น record/alert ไม่ auto-trade

แชร์ key/model/fallback เดียวกับ translate (gemini-2.5-flash → flash-latest)
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
_FALLBACK_MODEL = "gemini-flash-latest"
_NEWS_AI_CACHE = "news_ai_cache.json"
_NEWS_AI_KEEP_DAYS = 30


def _key() -> str:
    return os.getenv("GEMINI_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def _call(prompt: str, schema: Optional[dict], temperature: float, model: str) -> Optional[object]:
    try:
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        gen: dict = {"temperature": temperature}
        if schema is not None:
            gen["response_mime_type"] = "application/json"
            gen["response_schema"] = schema
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
        r = requests.post(url, params={"key": _key()}, json=body, timeout=45)
        if r.status_code != 200:
            log.warning("gemini ai (%s) → %s %s", model, r.status_code, r.text[:140])
            return None
        txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(txt) if schema is not None else txt
    except Exception as e:  # noqa: BLE001
        log.warning("gemini ai failed: %s", e)
        return None


def gemini(prompt: str, schema: Optional[dict] = None, temperature: float = 0.3) -> Optional[object]:
    """เรียก Gemini — schema=None → คืน str; มี schema → คืน dict/list (JSON)
    ลองรุ่นหลักก่อน, พลาดลอง flash-latest, พลาดอีกคืน None (caller fallback เอง)"""
    if not enabled():
        return None
    for model in (_MODEL, _FALLBACK_MODEL):
        out = _call(prompt, schema, temperature, model)
        if out is not None:
            return out
    return None


# ── cache (per-id, สำหรับวิเคราะห์ข่าว) ───────────────────────────────
def _cache_load(name: str) -> dict:
    try:
        from watchlist import store
        return store.load_json(name, {}) or {}
    except Exception:  # noqa: BLE001
        return {}


def _cache_save(name: str, c: dict) -> None:
    try:
        from watchlist import store
        store.save_json(name, c)
    except Exception as e:  # noqa: BLE001
        log.warning("ai cache save failed: %s", e)


_NEWS_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "relevant": {"type": "BOOLEAN"},
            "th": {"type": "STRING"},
            "dir": {"type": "STRING", "enum": ["up", "down", "flat"]},
            "cluster": {"type": "INTEGER"},
        },
        "required": ["id", "relevant", "th", "dir", "cluster"],
    },
}


def analyze_news(items: list[dict]) -> dict:
    """วิเคราะห์ข่าว batch → {id: {relevant, th, dir, cluster}}
      items: [{'id','symbol','headline','summary'}]
      - relevant: ข่าวกระทบหุ้นตัวนั้นจริงไหม (กรองข่าว sector/ไม่เกี่ยว)
      - th: สรุปไทย 1-2 ประโยค (เกิดอะไร + สำคัญยังไง)
      - dir: up/down/flat (จากเนื้อข่าวจริง ไม่ใช่ keyword)
      - cluster: เลขกลุ่มข่าวเรื่องเดียวกัน (รวมข่าวซ้ำ)
    cache ต่อ id (relevant/th/dir); cluster คำนวณใหม่ทุกรอบ (เฉพาะ batch นี้)
    คืน {} ถ้า Gemini ใช้ไม่ได้ → caller ใช้ทางเดิม (Google Translate)"""
    result: dict = {}
    if not items or not enabled():
        return result
    cache = _cache_load(_NEWS_AI_CACHE)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    todo = [it for it in items if str(it.get("id")) not in cache]
    for it in items:  # เติมจาก cache ก่อน
        ent = cache.get(str(it.get("id")))
        if ent:
            result[str(it["id"])] = {k: ent[k] for k in ("relevant", "th", "dir")}
            result[str(it["id"])]["cluster"] = -1  # cached = ไม่จัดกลุ่มข้ามรอบ

    changed = False
    # แบ่ง chunk (≤12/ครั้ง) กัน 1 call ใหญ่เกินจน timeout · offset cluster กันชนข้าม chunk
    chunk_size = 12
    for ci in range(0, len(todo), chunk_size):
        chunk = todo[ci:ci + chunk_size]
        payload = [{"id": str(it["id"]), "symbol": it.get("symbol", ""),
                    "headline": it.get("headline", ""), "summary": (it.get("summary") or "")[:300]}
                   for it in chunk]
        prompt = (
            "คุณเป็นนักวิเคราะห์หุ้น วิเคราะห์ข่าวต่อไปนี้ (แต่ละชิ้นระบุ symbol หุ้นที่เกี่ยวข้อง) "
            "สำหรับแต่ละข่าว ให้:\n"
            "1) relevant: ข่าวนี้ 'กระทบหุ้น symbol นั้นโดยตรง' จริงไหม (true/false) — "
            "ข่าวที่แค่พาดพิงบริษัทอื่น/ภาพรวมตลาดกว้าง ๆ ที่ไม่ได้เจาะ symbol นี้ ให้ false\n"
            "2) th: สรุปเป็นไทยสั้น 1-2 ประโยค บอกว่าเกิดอะไรและสำคัญต่อหุ้นยังไง (คงชื่อบริษัท/ตัวย่อเป็นอังกฤษ)\n"
            "3) dir: ผลต่อราคาหุ้นน่าจะ up / down / flat (จากเนื้อข่าวจริงเท่านั้น ห้ามเดาเกินข้อมูล)\n"
            "4) cluster: เลขจำนวนเต็ม ข่าวที่เป็น 'เรื่องเดียวกัน' ให้เลข cluster เดียวกัน (รวมข่าวซ้ำจากหลายสำนัก)\n"
            "ตอบเป็น JSON array ตาม schema เท่านั้น ห้ามแต่งข้อมูลที่ไม่มีในข่าว:\n"
            + json.dumps(payload, ensure_ascii=False)
        )
        out = gemini(prompt, schema=_NEWS_SCHEMA, temperature=0.2)
        if not isinstance(out, list):
            continue
        for row in out:
            rid = str(row.get("id", ""))
            if not rid:
                continue
            raw_cl = row.get("cluster", -1)
            cl = (raw_cl + ci * 1000) if isinstance(raw_cl, int) and raw_cl >= 0 else -1
            rec = {"relevant": bool(row.get("relevant", True)),
                   "th": (row.get("th") or "").strip(),
                   "dir": row.get("dir") if row.get("dir") in ("up", "down", "flat") else "flat"}
            result[rid] = {**rec, "cluster": cl}
            if rec["th"]:
                cache[rid] = {**rec, "ts": now}
                changed = True
    if changed:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=_NEWS_AI_KEEP_DAYS)).isoformat(timespec="seconds")
        cache = {k: v for k, v in cache.items() if (v.get("ts") or "") >= cutoff}
        _cache_save(_NEWS_AI_CACHE, cache)
    return result
