"""
core/alloc.py — แบ่งสัดส่วนเงินระหว่างหุ้นที่ติดอันดับ (position sizing)

ตอบคำถามเดียว: "มีเงินก้อนหนึ่งที่ตั้งใจแบ่งมาลงกับ Top N ตัวนี้ ควรใส่ตัวละกี่ %"
→ เป็นสัดส่วน *ภายในก้อนนั้น* ไม่ใช่ % ของพอร์ตทั้งหมด
  (บอทไม่รู้เงินทั้งหมด/ของที่ถืออยู่ที่อื่น/ความเสี่ยงที่รับได้ของผู้ใช้ — บอกได้แค่สัดส่วนภายในก้อน)

หลักการ: ถ่วงด้วย "ระยะถึง SL" ไม่ใช่ถ่วงด้วยความมั่นใจ
  บอทตั้ง SL ที่ ราคา − 2×ATR ซึ่งแต่ละตัวห่างไม่เท่ากัน (4% vs 17% ก็มี)
  ถ้าใส่เงินเท่ากันหมด ตัวที่เหวี่ยงจะครองความเสี่ยงของก้อนทั้งหมดโดยไม่ตั้งใจ
  → น้ำหนัก ∝ 1 / ระยะ SL  ทำให้ "ถ้าโดน SL จะเจ็บเท่ากันทุกตัว" (equal risk)
  แล้วค่อยเอียงตามคะแนนเล็กน้อย (ปิดได้ด้วย tilt=0)

pure logic: ไม่มี I/O · ไม่ import อะไรในโปรเจกต์
"""
from __future__ import annotations
import math
from typing import Optional

# เพดาน/พื้นของน้ำหนักต่อตัว — คิดเป็น "กี่เท่าของน้ำหนักเท่ากัน (1/n)"
# ต้องผูกกับ n ไม่ใช่ตั้งเป็น % ตายตัว: เพดานตายตัว 35% ใช้กับ 2 ตัวไม่ได้ (2×35% = 70% ไม่ถึง 100%)
# และการผูกกับ 1/n ทำให้กรอบ "เป็นไปได้เสมอ" (n×min = 0.40 ≤ 1 ≤ n×max = 1.75) ทุกค่า n
# ที่ n=5 → เพดาน 35% พื้น 8%
MAX_MULT = 1.75
MIN_MULT = 0.40
DEFAULT_STOP_ATR = 2.0     # ต้องตรงกับ SL ที่ /top5 แสดง (price − 2×ATR)


def _finite_pos(v) -> Optional[float]:
    """คืน float ที่ใช้ได้จริง (ไม่ใช่ None/NaN/inf/ติดลบ) ไม่งั้นคืน None"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def stop_distance_pct(price, atr, stop_atr: float = DEFAULT_STOP_ATR) -> Optional[float]:
    """ระยะจากราคาถึง SL คิดเป็น % ของราคา — None ถ้าข้อมูลไม่พอ"""
    p, a = _finite_pos(price), _finite_pos(atr)
    if p is None or a is None:
        return None
    d = stop_atr * a / p * 100.0
    # ระยะที่เพี้ยนเกินจริง (ATR ใหญ่กว่าราคาครึ่งหนึ่ง) ไม่เอามาคิดน้ำหนัก
    return d if 0.3 <= d <= 60.0 else None


def _rebalance(raw: list) -> list:
    """แบ่งน้ำหนักตามสัดส่วน raw แล้วบีบให้อยู่ในกรอบ — ผลรวมเป็น 1 พอดีเสมอ

    วิธี water-filling: ตัวที่ทะลุเพดานถูกตรึงไว้ที่เพดาน แล้วเอาส่วนที่เหลือ
    ไปเกลี่ยให้ตัวที่ยังอิสระตามสัดส่วนเดิม วนจนไม่มีใครชนกรอบ

    สำคัญ 2 อย่าง:
      · จัดการทีละด้าน (ทะลุเพดานก่อน แล้วค่อยต่ำกว่าพื้น) — ถ้าตรึงพร้อมกันสองด้าน
        จะตรึงครบทุกตัวจนไม่เหลือใครรับส่วนต่าง แล้วผลรวมไม่เท่า 1
      · ห้าม normalize ทับตอนท้าย — จะดันตัวที่ตรึงไว้ทะลุเพดานที่เพิ่งบีบไป
    เพราะกรอบผูกกับ 1/n จึงพิสูจน์ได้ว่าจะไม่มีทางที่ทุกตัวชนกรอบพร้อมกัน → วนจบเสมอ
    """
    n = len(raw)
    if n == 0:
        return []
    max_w, min_w = MAX_MULT / n, MIN_MULT / n
    base = [r if (isinstance(r, float) and math.isfinite(r) and r > 0) else 0.0 for r in raw]
    if sum(base) <= 0:
        return [1.0 / n] * n

    fixed: dict = {}
    free = list(range(n))
    for _ in range(2 * n + 2):
        if not free:
            break
        rem = 1.0 - sum(fixed.values())
        s = sum(base[i] for i in free)
        cur = {i: (base[i] / s * rem if s > 0 else rem / len(free)) for i in free}
        over = [i for i in free if cur[i] > max_w + 1e-12]
        if over:
            for i in over:
                fixed[i] = max_w
                free.remove(i)
            continue
        under = [i for i in free if cur[i] < min_w - 1e-12]
        if under:
            for i in under:
                fixed[i] = min_w
                free.remove(i)
            continue
        fixed.update(cur)
        break
    return [fixed.get(i, 0.0) for i in range(n)]


def _to_whole_pct(weights: list) -> list:
    """ทศนิยม → จำนวนเต็ม % ที่รวมได้ 100 พอดี (largest remainder)
    ต้องรวมได้ 100 เป๊ะ ไม่งั้นผู้ใช้บวกเลขตามแล้วไม่ลงตัว"""
    n = len(weights)
    if n == 0:
        return []
    scaled = [w * 100 for w in weights]
    base = [int(math.floor(x)) for x in scaled]
    left = 100 - sum(base)
    order = sorted(range(n), key=lambda i: -(scaled[i] - base[i]))
    for k in range(max(0, left)):
        base[order[k % n]] += 1
    return base


def allocate(picks: list, *, stop_atr: float = DEFAULT_STOP_ATR, tilt: float = 0.5) -> dict:
    """แบ่งสัดส่วนเงินให้ picks (list ของ dict ที่มี price/atr/score)

    tilt = เอียงตามคะแนนแค่ไหน (0 = ไม่เอียงเลย ใช้ความเสี่ยงล้วน · 0.5 = ค่าเริ่มต้น เอียงเบา ๆ)
    คืน {"weights": [int %], "risk_pct": [ระยะ SL ของแต่ละตัว], "total_risk_pct": float|None,
         "basis": "risk"|"equal"}
    """
    n = len(picks or [])
    if n == 0:
        return {"weights": [], "risk_pct": [], "total_risk_pct": None, "basis": "equal"}

    dist = [stop_distance_pct(p.get("price"), p.get("atr"), stop_atr) for p in picks]
    known = [d for d in dist if d is not None]
    if not known:
        # ไม่มี ATR ใช้ได้เลย → เกลี่ยเท่ากัน ตรงไปตรงมาดีกว่าเดา
        w = _to_whole_pct([1.0 / n] * n)
        return {"weights": w, "risk_pct": dist, "total_risk_pct": None, "basis": "equal"}

    med = sorted(known)[len(known) // 2]      # ตัวที่ไม่มี ATR ใช้ค่ากลางแทน (ไม่ให้ได้เปรียบ/เสียเปรียบ)
    raw = []
    for p, d in zip(picks, dist):
        risk = d if d is not None else med
        sc = _finite_pos(p.get("score")) or 50.0
        # เอียงตามคะแนน: tilt=0.5 → ตัวคะแนน 90 ได้น้ำหนักมากกว่าตัวคะแนน 60 ราว 15%
        factor = 1.0 + tilt * (min(sc, 100.0) - 70.0) / 100.0
        raw.append(max(factor, 0.1) / risk)

    weights = _rebalance(raw)
    pct = _to_whole_pct(weights)
    # ถ้าโดน SL ทุกตัวพร้อมกัน จะเสียกี่ % ของก้อนนี้ — ตัวเลขที่สำคัญที่สุด
    total_risk = sum((w / 100.0) * (d if d is not None else med) for w, d in zip(pct, dist))
    return {"weights": pct, "risk_pct": dist,
            "total_risk_pct": round(total_risk, 1), "basis": "risk"}
