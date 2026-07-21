"""
core/ranking.py — คะแนน "น่าซื้อ" (buy_score 0-100) สำหรับจัดอันดับ Top picks

โจทย์: หา "หุ้นที่อยู่ *ต้นเทรนด์*" (น่าเข้า) และกด "ปลายเทรนด์" (ควรออก) ลง
ต่างจาก setup_quality ของ core/signals.py ที่ตอบ "setup ตอนนี้คุณภาพดีไหม"
→ ที่นี่ตอบ "อยู่ช่วงไหนของเทรนด์" แล้ว *ยืม* setup_score มาเป็น 1 องค์ประกอบ (ไม่คำนวณซ้ำ)

pure logic: ไม่มี I/O · ไม่ import main/store/network · ใช้เฉพาะ field ที่ Signal มีจริง
"""
from __future__ import annotations

# โซนที่ยอมให้เข้ารอบฝั่งซื้อ (green=ขาขึ้น · yellow=ย่อตื้น=จังหวะเข้า · blue=ปลายขาลงเด้งแรง=ดักกลับตัว)
_BUY_ZONES = ("green", "yellow", "blue")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def passes_gate(s, *, min_adx: float = 18.0, min_rs: float = 55.0) -> bool:
    """ด่านคัดก่อนให้คะแนน — ผ่อนกว่า hard filter ของสแกนปกติ เพื่อให้ pool ใหญ่พอ
    ค่า None = "ไม่รู้" → ปล่อยผ่าน (ไม่ลงโทษข้อมูลขาด)"""
    if getattr(s, "zone", None) not in _BUY_ZONES:
        return False
    stage = getattr(s, "stage", None) or {}
    if stage.get("n") == 4:                       # Stage 4 = ใต้ MA150 ที่ชี้ลง — ห้ามฝั่งซื้อ
        return False
    adx = getattr(s, "adx", None)
    if adx is not None and adx < min_adx:
        return False
    rs = getattr(s, "rs_rank", None)
    if rs is not None and rs < min_rs:
        return False
    return True


def buy_score(s) -> "tuple[float, dict]":
    """คืน (คะแนน 0-100, รายละเอียดแต่ละองค์ประกอบ) — ยิ่งสูง = ยิ่งอยู่ต้นเทรนด์+คุณภาพดี"""
    # ── A) ต้นเทรนด์ 40 ────────────────────────────────────────────────
    ext = getattr(s, "ext_atr", None)
    a1 = 9.0 if ext is None else 18.0 * _clamp((3.0 - ext) / 2.5, 0.0, 1.0)

    zone, prev = getattr(s, "zone", None), getattr(s, "prev_zone", None)
    if zone == "green":
        a2 = 12.0 if prev in ("blue", "lblue", "red") else (
            10.0 if prev in ("yellow", "orange") else 7.0)
    elif zone == "yellow":
        a2 = 8.0
    elif zone == "blue":
        a2 = 6.0
    else:
        a2 = 0.0

    biz = getattr(s, "bars_in_zone", None)
    a3 = 5.0 if biz is None else 10.0 * _clamp((25.0 - biz) / 25.0, 0.0, 1.0)
    A = a1 + a2 + a3

    # ── B) Relative Strength 20 ───────────────────────────────────────
    rs = getattr(s, "rs_rank", None)
    B = 10.0 if rs is None else 20.0 * _clamp(rs / 100.0, 0.0, 1.0)

    # ── C) Weinstein Stage 18 (ขั้น 12 + ความ "สด" ของ MA150 6) ────────
    stage = getattr(s, "stage", None) or {}
    n = stage.get("n")
    c1 = {2: 12.0, 1: 7.0, 3: 3.0, 4: 0.0}.get(n, 6.0)
    slope = stage.get("slope_pct")
    if slope is None:
        c2 = 3.0
    elif slope <= 0:
        c2 = 0.0
    elif slope < 1:
        c2 = 4.0          # เพิ่งพลิกเงย
    elif slope <= 5:
        c2 = 6.0          # ต้น-กลาง Stage 2 = ช่วงทอง
    elif slope <= 8:
        c2 = 3.0
    else:
        c2 = 1.0          # ร้อนเกิน = ปลายเทรนด์
    C = c1 + c2

    # ── D) คุณภาพ setup 15 — ยืม setup_score (price action 14 มิติ) ────
    ss = getattr(s, "setup_score", None)
    D = 7.5 if ss is None else 15.0 * _clamp((ss + 3.0) / 7.0, 0.0, 1.0)

    # ── E) แรงเงิน/โมเมนตัม 7 ─────────────────────────────────────────
    vr = getattr(s, "vol_ratio", None)
    e1 = 2.0 if vr is None else (4.0 if vr >= 1.5 else (2.5 if vr >= 1.0 else 0.0))
    adx = getattr(s, "adx", None)
    e2 = 1.5 if adx is None else 3.0 * _clamp((adx - 18.0) / 22.0, 0.0, 1.0)
    E = e1 + e2

    # ── หัก "ปลายเทรนด์" ──────────────────────────────────────────────
    pen = 0.0
    rsi = getattr(s, "rsi", None)
    if rsi is not None:
        if rsi >= 75:
            pen += 20.0
        elif rsi >= 70:
            pen += 10.0
    if ext is not None and ext >= 3.0:
        pen += 5.0
    if n == 3:
        pen += 8.0
    if getattr(s, "vol_above", None) is False and (vr is not None and vr < 0.7):
        pen += 5.0

    total = _clamp(A + B + C + D + E - pen, 0.0, 100.0)
    parts = {"early": round(A, 1), "rs": round(B, 1), "stage": round(C, 1),
             "setup": round(D, 1), "flow": round(E, 1), "penalty": -round(pen, 1)}
    return round(total, 1), parts


_PART_TH = {"early": "ต้นเทรนด์", "rs": "แข็งกว่าตลาด", "stage": "Stage ดี",
            "setup": "setup สวย", "flow": "แรงเงินเข้า"}


def top_reasons(parts: dict, k: int = 3) -> "list[str]":
    """3 องค์ประกอบที่ให้คะแนนสูงสุด — ไว้บอก 'ทำไมถึงติดอันดับ'"""
    pos = [(v, _PART_TH[key]) for key, v in (parts or {}).items() if key in _PART_TH and v > 0]
    pos.sort(reverse=True)
    return [name for _, name in pos[:k]]


def late_flags(s) -> "list[str]":
    """ธง 'ปลายเทรนด์ — ควรพิจารณาออก' (ใช้ทั้งตอนคัดเข้าและตอนเตือนขาย = เกณฑ์เดียวกัน)"""
    out: list[str] = []
    ext = getattr(s, "ext_atr", None)
    if ext is not None and ext >= 3.0:
        out.append(f"ยืดจาก EMA12 {ext:.1f} ATR (ไล่ของแพง)")
    rsi = getattr(s, "rsi", None)
    if rsi is not None and rsi >= 70:
        out.append(f"RSI {rsi:.0f} ร้อน")
    stage = getattr(s, "stage", None) or {}
    if stage.get("n") == 3:
        out.append("Stage 3 — ทำจุดสูง/แจกของ")
    sl = stage.get("slope_pct")
    if sl is not None and sl > 8:
        out.append("MA150 ชันเกิน (ร้อนเกิน)")
    if getattr(s, "zone", None) in ("yellow", "orange") and getattr(s, "prev_zone", None) == "green":
        out.append("โซนพลิกจากขาขึ้น — เริ่มย่อ")
    return out
