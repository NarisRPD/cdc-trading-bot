"""
Offline test สำหรับ universe/sector_map.diversify — จำกัดจำนวนหุ้นต่อเซกเตอร์

เคสจริงที่ทำให้ต้องมีฟีเจอร์นี้: Top 5 ออกมาเป็น Healthcare 4 ตัว
(VCYT/CORT/MOH/UNH) เพราะ rs_rank วัดเทียบทั้งตลาด เซกเตอร์ที่นำจึงกวาดอันดับไปหมด

สิ่งที่ต้องไม่พัง:
  1) จำกัดได้จริง และยังเรียงตามคะแนนภายในกรอบ
  2) ตลาดแคบจนหาไม่ครบ n → ต้องได้ครบ n อยู่ดี (ยอมเกินโควตา ดีกว่าส่งไม่ครบ)
  3) เซกเตอร์ที่ไม่รู้ (None) ห้ามถูกจำกัด — ข้อมูลขาดไม่ควรกลายเป็นการลงโทษ

วิธีรัน:  python tests\\test_sector_diversify.py
"""
from __future__ import annotations

import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from universe.sector_map import diversify  # noqa: E402

_FAIL: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")
    if not ok:
        _FAIL.append(name)


SEC = {"VCYT": "สุขภาพ", "CORT": "สุขภาพ", "MOH": "สุขภาพ", "UNH": "สุขภาพ",
       "ESS": "อสังหาฯ", "NVDA": "เทคโนโลยี", "AMD": "เทคโนโลยี", "JPM": "การเงิน",
       "XOM": "พลังงาน", "UNKNOWN1": None, "UNKNOWN2": None, "UNKNOWN3": None}
sec_of = lambda t: SEC.get(t)  # noqa: E731

print("[1] เคสจริงจากหน้าจอ — Healthcare 4 ตัว ต้องเหลือ 2")
ranked = ["VCYT", "CORT", "ESS", "MOH", "UNH", "NVDA", "JPM"]
picked, used = diversify(ranked, 5, 2, sec_of)
check("ได้ 5 ตัว", len(picked), 5)
check("เลือกตามคะแนนภายในกรอบ", picked, ["VCYT", "CORT", "ESS", "NVDA", "JPM"])
check("สุขภาพเหลือ 2", used.get("สุขภาพ"), 2)
check("ได้ 4 เซกเตอร์", len(used), 4)

print("\n[2] ตลาดแคบ — มีแต่ Healthcare ต้องได้ครบ 5 อยู่ดี")
picked, used = diversify(["VCYT", "CORT", "MOH", "UNH"], 5, 2, sec_of)
check("มีของแค่ 4 ก็ได้ 4", len(picked), 4)
check("ยังคงลำดับคะแนนเดิม", picked, ["VCYT", "CORT", "MOH", "UNH"])

picked, _ = diversify(["VCYT", "CORT", "MOH", "UNH", "ESS"], 5, 2, sec_of)
check("มี 5 ตัวให้เลือก → ต้องได้ครบ 5 (ยอมเกินโควตา)", len(picked), 5)
check("ตัวที่ถูกกันไว้ถูกดึงกลับตามลำดับคะแนน", picked, ["VCYT", "CORT", "ESS", "MOH", "UNH"])

print("\n[3] เซกเตอร์ที่ไม่รู้ ห้ามถูกจำกัด")
picked, used = diversify(["UNKNOWN1", "UNKNOWN2", "UNKNOWN3", "VCYT", "CORT"], 5, 2, sec_of)
check("ไม่รู้เซกเตอร์ 3 ตัวผ่านหมด", picked, ["UNKNOWN1", "UNKNOWN2", "UNKNOWN3", "VCYT", "CORT"])
check("ไม่นับเข้าโควตาเซกเตอร์ไหน", used, {"สุขภาพ": 2})

print("\n[4] ปิดฟีเจอร์ด้วย max_per_sector<=0")
picked, used = diversify(["VCYT", "CORT", "MOH", "UNH", "ESS"], 5, 0, sec_of)
check("เอาตามคะแนนล้วน", picked, ["VCYT", "CORT", "MOH", "UNH", "ESS"])
check("ยังนับเซกเตอร์ให้ดู", used, {"สุขภาพ": 4, "อสังหาฯ": 1})

print("\n[5] เข้มสุด 1 ตัว/เซกเตอร์")
picked, used = diversify(["VCYT", "CORT", "MOH", "ESS", "NVDA", "AMD", "JPM"], 5, 1, sec_of)
check("ได้ 5 เซกเตอร์ต่างกันหมด", len(set(SEC[t] for t in picked)), 4)
check("เลือกตัวแรกของแต่ละเซกเตอร์", picked[:4], ["VCYT", "ESS", "NVDA", "JPM"])

print("\n[6] ขอบ")
check("ranked ว่าง", diversify([], 5, 2, sec_of), ([], {}))
check("n=0", diversify(["VCYT"], 0, 2, sec_of), ([], {}))

print()
if _FAIL:
    print(f"❌ FAIL {len(_FAIL)} เคส: {', '.join(_FAIL)}")
    sys.exit(1)
print("✅ PASS — diversify (จำกัดต่อเซกเตอร์ · ได้ครบจำนวน · ไม่ลงโทษข้อมูลขาด)")
