"""
scalping_bot/_config.py — โหลด config.env (KEY=VALUE) แบบไม่พึ่ง dependency
ลำดับความสำคัญ: environment variable > config.env
*** config.env เก็บรหัสผ่าน — อยู่ใน .gitignore ห้าม commit ***
"""
from __future__ import annotations
import os


def load(path: str = "config.env") -> dict:
    cfg: dict = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                # ตัด inline comment: รองรับทั้ง "VALUE # note" และ "VALUE#note"
                # ใช้ regex แทน string find เพื่อรองรับทุกรูปแบบ
                import re as _re
                v = _re.sub(r'\s*#.*$', '', v)
                if v.strip():                # ไม่เก็บ empty value — ให้ cfg.get() fallback ไป default ได้
                    cfg[k.strip()] = v.strip()
    for k in list(cfg):           # env override (เผื่อ set ชั่วคราว)
        if os.getenv(k):
            cfg[k] = os.getenv(k)
    return cfg


def get(cfg: dict, key: str, default=None, cast=str):
    v = cfg.get(key, default)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except (ValueError, TypeError):
        return default
