"""
watchlist/store.py — เก็บรายการที่ถืออยู่ (positions) เป็น JSON

backend 2 แบบ (เลือกอัตโนมัติ):
- GCS: ถ้าตั้ง env WATCHLIST_BUCKET → เก็บใน Cloud Storage (โปรดักชัน)
- Local: ถ้าไม่ตั้ง → เก็บไฟล์ในโฟลเดอร์ WATCHLIST_LOCAL_DIR (ดีฟอลต์ .) สำหรับเทส

ไฟล์ที่ใช้: positions.json (พอร์ต) · settings.json (ทุน/ความเสี่ยง) · journal.json (ไม้ที่ปิด)
ใช้คนเดียว → read-modify-write ทั้งไฟล์พอ (concurrency ต่ำ)
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

_POSITIONS = "positions.json"


def _bucket_name() -> Optional[str]:
    b = os.getenv("WATCHLIST_BUCKET", "").strip()
    return b or None


def _local_path(name: str) -> str:
    return os.path.join(os.getenv("WATCHLIST_LOCAL_DIR", "."), name)


def load_json(name: str, default: Any) -> Any:
    """อ่าน JSON จาก GCS (หรือไฟล์ local) — คืน default ถ้าไม่มี/ว่าง"""
    bucket = _bucket_name()
    if bucket:
        from google.cloud import storage  # lazy import กัน dependency ตอนเทส local

        blob = storage.Client().bucket(bucket).blob(name)
        if not blob.exists():
            return default
        return json.loads(blob.download_as_text() or "null") or default
    path = _local_path(name)
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f) or default


def save_json(name: str, data: Any) -> None:
    """เขียน JSON ลง GCS (หรือไฟล์ local)"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    bucket = _bucket_name()
    if bucket:
        from google.cloud import storage

        blob = storage.Client().bucket(bucket).blob(name)
        blob.upload_from_string(payload, content_type="application/json")
        return
    with open(_local_path(name), "w", encoding="utf-8") as f:
        f.write(payload)


def position_key(data_ticker: str, side: str) -> str:
    return f"{data_ticker}|{side}"


def _load_raw() -> dict:
    return load_json(_POSITIONS, {})


def _save_raw(data: dict) -> None:
    save_json(_POSITIONS, data)


def list_positions() -> list[dict]:
    """คืน position ทั้งหมด (list ของ dict)"""
    return list(_load_raw().values())


def get_by_ticker(data_ticker: str) -> list[dict]:
    """คืน position ที่ตรง data_ticker (อาจมีหลาย side เช่นถือทั้ง call+put)"""
    return [p for p in _load_raw().values() if p.get("symbol") == data_ticker]


def add_position(pos: dict) -> None:
    """เพิ่ม/ทับ position (key = ticker|side)"""
    data = _load_raw()
    data[position_key(pos["symbol"], pos["side"])] = pos
    _save_raw(data)


def remove_position(data_ticker: str, side: str) -> Optional[dict]:
    """ลบ position; คืน dict ที่ลบ หรือ None ถ้าไม่เจอ"""
    data = _load_raw()
    removed = data.pop(position_key(data_ticker, side), None)
    if removed is not None:
        _save_raw(data)
    return removed
