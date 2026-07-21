"""
Offline test สำหรับ core/alloc.py — สัดส่วนแบ่งเงินระหว่างหุ้นที่ติดอันดับ

โฟกัสสิ่งที่ "ผิดแล้วผู้ใช้เอาเงินจริงไปวางผิด":
  1) รวมต้องได้ 100 เป๊ะเสมอ (ผู้ใช้บวกตามแล้วต้องลงตัว)
  2) ตัวที่ SL ห่างกว่า ต้องได้เงินน้อยกว่า → โดน SL แล้วเจ็บพอ ๆ กัน
  3) ข้อมูลขาด/NaN/ATR เพี้ยน ต้องไม่ทำให้ตัวเลขบ้า
  4) เพดาน-พื้นน้ำหนักต้องไม่ถูกละเมิด

วิธีรัน:  python tests\\test_alloc.py
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

from core import alloc  # noqa: E402

_FAIL: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")
    if not ok:
        _FAIL.append(name)


def ok_(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        _FAIL.append(name)


def P(sym, price, atr, score=70):
    return {"symbol": sym, "price": price, "atr": atr, "score": score}


print("[1] รวมต้องได้ 100 เสมอ")
for case in (
    [P("A", 100, 2)],
    [P("A", 100, 2), P("B", 50, 3)],
    [P("A", 250, 5, 90), P("B", 46, 4, 61), P("C", 170, 4, 78), P("D", 30, 1, 66), P("E", 88, 2, 72)],
    [P("A", 100, 2) for _ in range(8)],
):
    r = alloc.allocate(case)
    ok_(f"n={len(case)} รวม=100", sum(r["weights"]) == 100, f"weights={r['weights']}")

print("\n[2] ตัวเหวี่ยงต้องได้เงินน้อยกว่า และเจ็บพอ ๆ กัน")
picks = [P("นิ่ง", 250, 5, 70),    # SL ห่าง 2*5/250 = 4%
         P("เหวี่ยง", 46, 4, 70)]  # SL ห่าง 2*4/46 = 17.4%
r = alloc.allocate(picks, tilt=0.0)
w = r["weights"]
ok_("ตัวนิ่งได้มากกว่าตัวเหวี่ยง", w[0] > w[1], f"{w[0]}% vs {w[1]}%")
hits = [w[i] / 100 * r["risk_pct"][i] for i in range(2)]
ok_("เจ็บใกล้เคียงกัน (ต่างกัน < 1.5 จุด)", abs(hits[0] - hits[1]) < 1.5,
    f"เสีย {hits[0]:.2f}% vs {hits[1]:.2f}% ของก้อน")
ok_("total_risk_pct = ผลรวมของทั้งสอง",
    abs(r["total_risk_pct"] - sum(hits)) < 0.15, f"{r['total_risk_pct']} vs {sum(hits):.2f}")

print("\n[3] เพดาน/พื้น — ผูกกับจำนวนตัว (1.75× / 0.40× ของน้ำหนักเท่ากัน)")
for case in ([P("A", 1000, 1, 70), P("B", 20, 5, 70), P("C", 100, 2, 70)],
             [P("A", 250, 5), P("B", 46, 4), P("C", 170, 4), P("D", 30, 1), P("E", 88, 2)],
             [P(f"S{i}", 100 + i * 30, 1 + i, 60 + i * 3) for i in range(9)]):
    n = len(case)
    r = alloc.allocate(case, tilt=0.0)
    cap = alloc.MAX_MULT / n * 100 + 1     # +1 กันปัดเศษ
    flo = alloc.MIN_MULT / n * 100 - 1
    ok_(f"n={n} อยู่ในกรอบ [{flo:.0f}%, {cap:.0f}%]",
        max(r["weights"]) <= cap and min(r["weights"]) >= flo, f"{r['weights']}")
    ok_(f"n={n} รวม 100", sum(r["weights"]) == 100)

check("n=5 เพดานยังเป็น 35% ตามที่ตั้งใจ", round(alloc.MAX_MULT / 5 * 100), 35)
check("n=5 พื้นยังเป็น 8%", round(alloc.MIN_MULT / 5 * 100), 8)

print("\n[4] คะแนนเอียงน้ำหนักได้ แต่ไม่พลิกลำดับความเสี่ยง")
same = [P("สูง", 100, 2, 95), P("ต่ำ", 100, 2, 55)]
r0 = alloc.allocate(same, tilt=0.0)
r1 = alloc.allocate(same, tilt=0.5)
check("tilt=0 + ความเสี่ยงเท่ากัน → เท่ากัน", r0["weights"], [50, 50])
ok_("tilt=0.5 → ตัวคะแนนสูงได้มากกว่า", r1["weights"][0] > r1["weights"][1], f"{r1['weights']}")

print("\n[5] ข้อมูลขาด/เพี้ยน ต้องไม่พัง")
check("ไม่มี pick เลย", alloc.allocate([])["weights"], [])
r = alloc.allocate([{"symbol": "A"}, {"symbol": "B"}])
check("ไม่มี price/atr เลย → เกลี่ยเท่ากัน", r["weights"], [50, 50])
check("basis บอกว่า equal", r["basis"], "equal")
check("ไม่มีข้อมูลก็ไม่กล้าบอก total risk", r["total_risk_pct"], None)

r = alloc.allocate([P("A", 100, 2), P("B", float("nan"), 3), P("C", 100, None)])
ok_("NaN/None ปนมา → ยังรวมได้ 100", sum(r["weights"]) == 100, f"{r['weights']}")
ok_("ไม่มีน้ำหนักติดลบ", all(x >= 0 for x in r["weights"]), f"{r['weights']}")

check("atr ใหญ่เกินจริง (>60% ของราคา) → ไม่เอามาคิด",
      alloc.stop_distance_pct(100, 40), None)
check("atr เล็กเกินจนไร้ความหมาย → ไม่เอามาคิด",
      alloc.stop_distance_pct(100, 0.1), None)
check("ราคา 0 → None", alloc.stop_distance_pct(0, 2), None)
check("ระยะ SL ปกติ", alloc.stop_distance_pct(250, 5), 4.0)

print("\n[6] ตัวอย่างจริง 5 ตัว")
real = [P("NVDA", 172.41, 4.2, 91), P("AMD", 163.2, 5.1, 78),
        P("AVGO", 251.0, 5.0, 66), P("PLTR", 45.7, 2.6, 61), P("MSFT", 430.0, 6.0, 74)]
r = alloc.allocate(real)
for p, wt, d in zip(real, r["weights"], r["risk_pct"]):
    print(f"     {p['symbol']:5} คะแนน {p['score']:2}  SL ห่าง {d:4.1f}%  → ใส่ {wt:2}%  "
          f"(โดน SL เสีย {wt/100*d:.2f}% ของก้อน)")
print(f"     รวม {sum(r['weights'])}% · โดน SL ครบทุกตัว = เสีย {r['total_risk_pct']}% ของก้อน")
ok_("รวม 100", sum(r["weights"]) == 100)
ok_("ความเสี่ยงรวมสมเหตุผล (3-15%)", 3 <= r["total_risk_pct"] <= 15, f"{r['total_risk_pct']}%")

print()
if _FAIL:
    print(f"❌ FAIL {len(_FAIL)} เคส: {', '.join(_FAIL)}")
    sys.exit(1)
print("✅ PASS — alloc (รวม 100 · ถ่วงด้วยความเสี่ยง · เพดาน/พื้น · ข้อมูลขาด)")
