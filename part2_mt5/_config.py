"""
part2_mt5/_config.py — โหลด config.env (KEY=VALUE) แบบไม่พึ่ง dependency
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
                # ตัด inline comment เช่น  KEY=180  # หมายเหตุ  → เก็บแค่ 180
                if " #" in v:
                    v = v[: v.index(" #")]
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
