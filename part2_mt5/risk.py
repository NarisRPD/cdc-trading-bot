"""
part2_mt5/risk.py — Risk Management ขั้นมืออาชีพ (หัวใจของ Part 2)

หลักการ: ความเสี่ยงคือ edge จริง ไม่ใช่สัญญาณ
- ขนาดไม้คำนวณจาก "%เสี่ยงต่อพอร์ต" + ระยะ SL (ไม่ใช่ความรู้สึก)
- R:R ต้องคุ้ม · มีเพดานไม้เปิด + เพดานขาดทุนต่อวัน (circuit breaker)
ไม่พึ่ง MT5 — คณิตล้วน (mt5_client จะป้อน balance/contract spec ให้)
"""
from __future__ import annotations
from typing import Optional


def rr(entry: float, sl: float, tp: float) -> Optional[float]:
    """อัตราส่วนผลตอบแทน:ความเสี่ยง (reward/risk)"""
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return abs(tp - entry) / risk


def risk_amount(balance: float, risk_pct: float) -> float:
    """จำนวนเงินที่ยอมเสียต่อไม้ (= balance × risk%)"""
    return balance * risk_pct / 100.0


def position_units(balance: float, risk_pct: float, entry: float, sl: float) -> float:
    """จำนวน 'หน่วย' ที่ทำให้ขาดทุนที่ SL = balance×risk% พอดี
    (mt5_client เอาไปแปลงเป็น lot ตาม contract size ของแต่ละสินทรัพย์)
    units × ระยะ SL = เงินเสี่ยง → units = เงินเสี่ยง / ระยะ SL"""
    dist = abs(entry - sl)
    if dist <= 0:
        return 0.0
    return risk_amount(balance, risk_pct) / dist


def lots_from_units(units: float, contract_size: float) -> float:
    """แปลงหน่วย → lot (เช่น ทอง 1 lot = 100 oz → contract_size=100)"""
    if contract_size <= 0:
        return 0.0
    return units / contract_size


def gate(*, rr_val: Optional[float], min_rr: float,
         open_positions: int, max_positions: int,
         day_loss_pct: float, max_daily_loss_pct: float) -> dict:
    """ด่านกฎความเสี่ยงพอร์ต — คืน {ok, reasons[]}
    ผ่านทุกข้อ → ออกใบสั่งได้ · ติดข้อใด = บอกเหตุผล ไม่ออกใบสั่ง (กันความผิดพลาดระบบ)"""
    reasons: list[str] = []
    if rr_val is not None and rr_val < min_rr:
        reasons.append(f"R:R {rr_val:.2f} ต่ำกว่าเกณฑ์ {min_rr} (เป้าใกล้กว่าความเสี่ยง = ไม่คุ้ม)")
    if open_positions >= max_positions:
        reasons.append(f"ไม้เปิดครบเพดานแล้ว ({open_positions}/{max_positions})")
    if day_loss_pct >= max_daily_loss_pct:
        reasons.append(f"ขาดทุนวันนี้ {day_loss_pct:.1f}% ถึงเพดาน {max_daily_loss_pct}% — หยุดเทรดวันนี้")
    return {"ok": not reasons, "reasons": reasons}


def build_levels(entry: float, sl: float, *, min_rr: float = 1.5,
                 tp_rr: float = 2.0) -> dict:
    """ประกอบ TP จากระยะความเสี่ยง (R-based): TP = entry ± risk×tp_rr
    คืน {risk, tp, rr} — ใช้เมื่ออยากให้ TP อิง R:R ที่ต้องการ"""
    risk = abs(entry - sl)
    if risk <= 0:
        return {"risk": 0.0, "tp": None, "rr": None}
    up = sl < entry  # SL ใต้ราคา = ฝั่งซื้อ
    tp = entry + risk * tp_rr if up else entry - risk * tp_rr
    return {"risk": risk, "tp": tp, "rr": tp_rr}
