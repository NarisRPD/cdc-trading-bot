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


def spike_flags(s, *, min_dollar_vol_m: float = 5.0, min_atr_pct: float = 1.2) -> "list[str]":
    """ธง "ขาขึ้นหลอก/เข้าไม่ได้" — ตัดออกจาก Top picks ตั้งแต่ต้นทาง

    เคสที่ผู้ใช้เจอจริง: หุ้นประกาศได้สัญญาใหญ่ (RAMP ฯลฯ) → พุ่งวันเดียว
    กราฟกลายเป็น "ขาขึ้น" ทุกอินดิเคเตอร์ ทั้งที่ไม่มีเทรนด์จริงรองรับ
    วิธีจับ: ถ้ากำไร 20 วันเกือบทั้งหมดมาจาก "วันเดียว" = ข่าว ไม่ใช่เทรนด์
    ค่า None = ไม่รู้ → ไม่ตั้งธง (ปรัชญาเดิม: ข้อมูลขาดไม่ใช่ความผิด)
    """
    out: list[str] = []
    up, r20 = getattr(s, "max_day_up_pct", None), getattr(s, "ret20_pct", None)
    up60 = getattr(s, "max_day_up60_pct", None)
    if up60 is not None and up60 >= 18.0:
        # วันเดียว +18% ภายใน 3 เดือน = เกือบแน่ว่าข่าวใหญ่/ดีลซื้อกิจการ — เทรนด์แท้ไม่ขึ้นแบบนี้
        # ต้องมอง 60 แท่งเพราะเคสจริง (RAMP): spike เกิดเดือนกว่าแล้ว หน้าต่าง 20 แท่งมองไม่เห็น
        # แต่ราคายังค้างสูง ทุกอินดิเคเตอร์เลยชี้ "ขาขึ้น" ทั้งที่ upside โดนล็อกไปแล้ว
        out.append(f"เคยพุ่งวันเดียว +{up60:.0f}% ใน 60 วัน (ข่าวใหญ่/ดีลซื้อกิจการ ไม่ใช่เทรนด์)")
    elif up is not None and up >= 9.0 and r20 is not None and r20 > 0 and up >= 0.7 * r20:
        # กำไรทั้งเดือน ≥70% มาจากวันเดียว — ที่เหลือแทบไม่ขยับ = spike ล้วน
        out.append(f"กำไร 20 วัน (+{r20:.0f}%) มาจากวันเดียว +{up:.0f}% เกือบทั้งหมด")
    # ความผันผวนต่ำผิดปกติ — หุ้นที่จะ "ถือรันเทรนด์" ต้องมีเทรนด์ให้รัน
    # หุ้นติดดีลซื้อกิจการราคาไปเกาะราคาดีล เหลือแกว่ง <1%/วัน (เคสจริง: RAMP ATR 0.8%)
    # ต้องมีกฎ "แบนอย่างเดียวก็ตัด" ด้วย เพราะถ้าดีลประกาศเกิน 60 แท่ง หน้าต่าง spike มองไม่เห็นแล้ว
    # และห้ามปล่อยเข้าสูตรแบ่งเงินเด็ดขาด: ถ่วงด้วย 1/ความเสี่ยง จะเทเงินให้ตัวที่ตายแล้วมากสุดพอดี
    atr_v, close_v = getattr(s, "atr", None), getattr(s, "close", None)
    atr_pct = (atr_v / close_v * 100.0) if (atr_v and close_v and close_v > 0) else None
    if atr_pct is not None:
        if up60 is not None and up60 >= 12.0 and atr_pct < 1.5:
            # เคยพุ่งแรง + แบนสนิท = ลายเซ็นโดนซื้อกิจการชัดที่สุด (หุ้นปกติหลังพุ่งยังแกว่ง 2-5%)
            out.append(f"พุ่ง +{up60:.0f}% แล้วแบนสนิท (ATR {atr_pct:.1f}%/วัน) "
                       "— รูปแบบโดนซื้อกิจการ ราคาถูกตรึงแล้ว")
        elif atr_pct < min_atr_pct:
            out.append(f"นิ่งผิดปกติ (แกว่ง ~{atr_pct:.1f}%/วัน) — ไม่มีเทรนด์ให้รัน "
                       "(ลักษณะหุ้นติดดีลซื้อกิจการ/ราคาถูกตรึง)")
    dv = getattr(s, "dollar_vol_m", None)
    if dv is not None and dv < min_dollar_vol_m:
        # ซื้อขายเบาบาง — เข้าออกจริงลำบาก + โดนลากราคาง่าย
        out.append(f"สภาพคล่องต่ำ (ซื้อขายเฉลี่ย ${dv:.1f}M/วัน)")
    return out


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
