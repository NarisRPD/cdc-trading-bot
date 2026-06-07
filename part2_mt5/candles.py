"""
part2_mt5/candles.py — ตรวจจับแท่งเทียน "จังหวะเข้า" (entry trigger)

ทำงานบน DataFrame OHLC (คอลัมน์ open/high/low/close/volume) — ไม่พึ่ง MT5/Part 1
ตรวจที่ "แท่งล่าสุดที่ปิดแล้ว" เทียบกับแท่งก่อนหน้า

เน้นแพทเทิร์นที่ "เชื่อถือได้ + คำนวณตรง" สำหรับเทรดสั้น:
engulfing (กลืน), pin/hammer/shooting-star (หางปฏิเสธ), doji (ลังเล),
inside bar (สะสมก่อนระเบิด), marubozu (แท่งตัน = แรงล้วน)
"""
from __future__ import annotations


def _vals(row) -> tuple[float, float, float, float]:
    return float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])


def detect(df) -> list[dict]:
    """คืน list ของแพทเทิร์นที่เจอบนแท่งล่าสุด: [{name, dir(bull/bear/neutral), strength(1-2)}]"""
    if df is None or len(df) < 2:
        return []
    o, h, l, c = _vals(df.iloc[-1])
    po, ph, pl, pc = _vals(df.iloc[-2])

    rng = max(h - l, 1e-9)
    body = abs(c - o)
    upper = h - max(o, c)   # ไส้บน
    lower = min(o, c) - l   # ไส้ล่าง
    bull = c >= o
    out: list[dict] = []

    # Engulfing — แท่งปัจจุบันกลืนตัวก่อนหน้า (กลับตัว/ยืนยันแรง)
    if bull and pc < po and c >= po and o <= pc:
        out.append({"name": "Bullish Engulfing (กลืนขึ้น)", "dir": "bull", "strength": 2})
    if (not bull) and pc > po and c <= po and o >= pc:
        out.append({"name": "Bearish Engulfing (กลืนลง)", "dir": "bear", "strength": 2})

    # Pin bar / Hammer / Shooting star — หางยาวปฏิเสธราคา (แรงสวนกลับ)
    if lower >= 2 * body and lower > upper and body <= 0.4 * rng:
        out.append({"name": "Hammer/Pin (หางล่างยาว ปฏิเสธลง)", "dir": "bull", "strength": 2})
    if upper >= 2 * body and upper > lower and body <= 0.4 * rng:
        out.append({"name": "Shooting Star/Pin (หางบนยาว ปฏิเสธขึ้น)", "dir": "bear", "strength": 2})

    # Doji — ตัวเล็กมาก = ลังเล (เตือนกลับตัวถ้าอยู่ปลายเทรนด์)
    if body <= 0.1 * rng:
        out.append({"name": "Doji (ลังเล)", "dir": "neutral", "strength": 1})

    # Inside bar — high/low อยู่ในกรอบแท่งก่อน = สะสมพลังก่อนระเบิด
    if h <= ph and l >= pl:
        out.append({"name": "Inside Bar (สะสมก่อนเบรก)", "dir": "neutral", "strength": 1})

    # Marubozu — เกือบไม่มีไส้ = แรงซื้อ/ขายล้วน
    if body >= 0.9 * rng:
        out.append({"name": f"Marubozu ({'ขึ้น' if bull else 'ลง'}แรงล้วน)",
                    "dir": "bull" if bull else "bear", "strength": 2})

    return out


def confirms(df, direction: str) -> list[dict]:
    """กรองเฉพาะแท่งที่ 'ยืนยันทิศ' ที่ต้องการ (bull=ฝั่งซื้อ, bear=ฝั่งขาย)
    ใช้ตอนเช็กว่ามีจังหวะแท่งเทียนสนับสนุนทิศของสัญญาณไหม"""
    want = "bull" if direction == "buy" else "bear"
    return [p for p in detect(df) if p["dir"] in (want, "neutral")]
