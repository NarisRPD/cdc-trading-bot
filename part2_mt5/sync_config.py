"""
part2_mt5/sync_config.py — จัดระเบียบ + sync config.env ให้ตรงโครง config.example.env

ทำงานอัตโนมัติตอนบอท start (เรียกจาก interactive.main ก่อนโหลด config):
  1. ยึด "โครงสร้าง + comment" ทั้งหมดจาก config.example.env (มากับ git — อัปเดตทุก pull)
  2. "ค่าจริง" ของผู้ใช้ (รวม secret) จาก config.env เดิม — คงไว้เสมอ ไม่แตะ
  3. key ใหม่ใน example ที่ config.env ยังไม่มี → เติมให้พร้อมค่า default ของ example
  4. key ใน config.env ที่หายไปจาก example → ย้ายไปท้ายไฟล์ section "นอก example"
     *** ไม่ลบทิ้งอัตโนมัติ — กันพฤติกรรมบอทเปลี่ยนเงียบๆ ถ้า example ตก key โดยพลาด
         ผู้ใช้ตรวจ section นี้แล้วลบเองได้เลย ***
  5. บรรทัดที่ comment เป็น KEY=VALUE ไว้ (เช่นบัญชีเดโมเก่า) → เก็บไว้ section ท้ายไฟล์
  6. สำรองไฟล์เดิม → config.env.bak_sync ก่อนเขียนทุกครั้ง · เขียนแบบ atomic
  7. ผลลัพธ์เหมือนเดิม → ไม่แตะไฟล์เลย (no-op · ไม่สำรองซ้ำ)

ปลอดภัย: log เฉพาะ "ชื่อ key" เท่านั้น — ห้าม log ค่า (config.env มี secret)

รันมือ (ดูผลก่อนจริง):  python sync_config.py --dry
"""
from __future__ import annotations

import logging
import os
import re
import shutil

log = logging.getLogger("part2.sync_config")

_DIR = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.join(_DIR, "config.example.env")
_CONFIG = os.path.join(_DIR, "config.env")

_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_COMMENTED_KV_RE = re.compile(r"^#\s*[A-Za-z_][A-Za-z0-9_]*=.+")

_ORPHAN_HEADER = "# ══ คีย์นอก config.example.env (อาจถูกยกเลิกแล้ว — ตรวจแล้วลบเองได้) ══"
_KEEP_HEADER = "# ══ บรรทัดเดิมที่ comment ไว้ (เก็บให้อัตโนมัติ เช่นบัญชีสำรอง) ══"


def _split_value(raw: str) -> str:
    """แยกค่าออกจาก inline comment — ตรรกะเดียวกับ _config.load (ตัดตั้งแต่ # แรก)"""
    return re.sub(r"\s*#.*$", "", raw).strip()


def _parse_user(path: str) -> "tuple[dict, list[str]]":
    """อ่าน config.env ของผู้ใช้ → ({key: value}, [บรรทัด KEY=VALUE ที่ถูก comment ไว้])
    key ซ้ำ → ตัวท้ายชนะ (ตรงพฤติกรรม loader) — เท่ากับ auto-แก้ปัญหา key ซ้ำในตัว"""
    values: dict = {}
    commented: list = []
    if not os.path.exists(path):
        return values, commented
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
            s = line.strip()
            if _COMMENTED_KV_RE.match(s):
                commented.append(s)
                continue
            m = _KV_RE.match(s)
            if m:
                values[m.group(1)] = _split_value(m.group(2))
    return values, commented


def _build(example_path: str, user_values: dict, user_commented: list) -> "tuple[str, dict]":
    """สร้างเนื้อหา config.env ใหม่: โครง example + ค่าผู้ใช้ · คืน (text, summary)"""
    out: list = []
    seen: set = set()
    added: list = []

    with open(example_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n").rstrip("\r")
            m = _KV_RE.match(line.strip())
            if not m:
                out.append(line)            # comment / บรรทัดว่าง → คงโครงตาม example
                continue
            key = m.group(1)
            raw = m.group(2)
            ex_val = _split_value(raw)
            # comment ท้ายบรรทัดของ example (คำอธิบาย key) — คงไว้ให้อ่านง่าย
            cm = re.search(r"(\s*#.*)$", raw)
            comment = cm.group(1).strip() if cm else ""
            if key in seen:
                continue                    # example มี key ซ้ำเอง → เอาตัวแรก
            seen.add(key)
            if key in user_values:
                val = user_values[key]      # ค่าผู้ใช้ (รวม secret) ชนะเสมอ
            else:
                val = ex_val                # key ใหม่ → ใช้ default ของ example
                added.append(key)
            out.append(f"{key}={val}" + (f"   {comment}" if comment else ""))

    # key ของผู้ใช้ที่ไม่อยู่ใน example — เก็บไว้ใช้งานต่อใน section ท้าย (ไม่ลบเงียบ)
    orphans = [k for k in user_values if k not in seen]
    if orphans:
        out += ["", _ORPHAN_HEADER]
        out += [f"{k}={user_values[k]}" for k in orphans]

    # บรรทัด comment KEY=VALUE เดิมของผู้ใช้ (เช่นบัญชีเดโม) — กันหายตอน rebuild
    existing = set(out)
    keep = [c for c in user_commented if c not in existing]
    if keep:
        out += ["", _KEEP_HEADER]
        out += keep

    return "\n".join(out) + "\n", {"added": added, "orphans": orphans}


def sync(example_path: str = _EXAMPLE, config_path: str = _CONFIG,
         dry: bool = False) -> "dict | None":
    """sync config.env ตามโครง example · คืน summary ถ้ามีการเปลี่ยน · None = ไม่มีอะไรเปลี่ยน
    ผิดพลาดใดๆ → โยน exception (ผู้เรียกครอบ try เอง — ห้ามทำบอทล่ม)"""
    if not os.path.exists(example_path):
        return None                          # ไม่มี example → ไม่ทำอะไร
    user_values, user_commented = _parse_user(config_path)
    if not user_values:
        return None                          # config.env ว่าง/ไม่มี → ไม่แตะ (กันเขียนทับโดยไม่ตั้งใจ)

    new_text, summary = _build(example_path, user_values, user_commented)

    old_text = ""
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            old_text = f.read()
    if new_text == old_text:
        return None                          # เหมือนเดิมทุกตัวอักษร → no-op

    if dry:
        return summary

    # สำรองก่อนเขียนเสมอ + เขียน atomic (เขียน tmp แล้วค่อย replace — ไฟล์ไม่มีวันครึ่งๆ กลางๆ)
    if os.path.exists(config_path):
        shutil.copy2(config_path, config_path + ".bak_sync")
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
    os.replace(tmp, config_path)
    return summary


if __name__ == "__main__":
    import sys
    # คอนโซล Windows ดีฟอลต์ cp1252/cp874 — พิมพ์ไทยแล้ว UnicodeEncodeError
    # บังคับ stdout เป็น UTF-8 (Python ≥3.7) · ถ้าไม่ได้ก็แทนตัวที่พิมพ์ไม่ได้ด้วย ?
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _dry = "--dry" in sys.argv
    r = sync(dry=_dry)
    if r is None:
        print("config.env ตรงกับ example แล้ว — ไม่มีอะไรต้องเปลี่ยน")
    else:
        mode = "[DRY-RUN ยังไม่เขียนจริง] " if _dry else ""
        print(f"{mode}เติมคีย์ใหม่ {len(r['added'])}: {', '.join(r['added']) or '-'}")
        print(f"{mode}คีย์นอก example (ย้ายไปท้ายไฟล์ ยังใช้งานได้) {len(r['orphans'])}: "
              f"{', '.join(r['orphans']) or '-'}")
