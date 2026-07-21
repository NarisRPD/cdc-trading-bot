"""
core/forward.py — ตรรกะ "หาแท่งถัดจากแท่งสัญญาณ" ที่ใช้ร่วมกันระหว่าง forward-test ทุกตัว

ทำไมต้องแยกออกมา: signals_log.py (วัดสัญญาณ) กับ watchlist/picks.py (วัดคำแนะนำ Top N)
ต้องใช้กติกาเดียวกันเป๊ะ ๆ ถ้าก๊อปโค้ดไปคนละไฟล์ บั๊กตระกูล a9b2287
("expired ด่วน" / "ข้ามแท่ง forward แรก") จะต้องไล่แก้ 2 ที่ และเทสคุมแค่ที่เดียว

pure logic: ไม่มี I/O · ไม่แตะ store/network
"""
from __future__ import annotations
import math
from typing import Optional, Tuple

import pandas as pd

# สถานะการหาแท่งเข้า
OK = "ok"                  # หาเจอ — ประเมินต่อได้
PENDING = "pending"        # แท่ง forward ยังไม่เกิด — รอรอบหน้า (อย่าตัดสินผล)
IMPOSSIBLE = "impossible"  # แท่งสัญญาณหลุด window ของ feed ไปแล้ว — ประเมินไม่ได้ตลอดกาล


def normalized_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    """index ของ DataFrame → DatetimeIndex แบบ tz-naive + normalize (ตัดเวลาเหลือแต่วัน)
    yfinance/ccxt คืน index มาไม่เหมือนกัน (บางทีมี tz บางทีมีเวลา) ต้องปรับให้เทียบกันได้ก่อน"""
    return pd.DatetimeIndex([
        (pd.Timestamp(x).tz_localize(None) if pd.Timestamp(x).tzinfo else pd.Timestamp(x)).normalize()
        for x in df.index
    ])


def forward_start(idx: pd.DatetimeIndex, bar_date) -> Tuple[Optional[int], str]:
    """แท่งสัญญาณวันที่ bar_date → index ของ "แท่ง forward แท่งแรก" (แท่งที่ถือจริงเริ่มนับ)

    คืน (start, status):
      · ตรงวันพอดี      → start = pos+1  (แท่งสัญญาณอยู่ที่ pos, ถือเริ่มแท่งถัดไป)
      · ไม่ตรง (วันสัญญาณเป็นวันหยุด / feed revise ทิ้งแท่งนั้น)
                        → idx[pos] คือแท่งแรกหลัง bar_date = แท่ง forward แท่งแรกอยู่แล้ว
                          → start = pos  (ห้ามใช้ pos+1 จะข้ามแท่งแรกไป = ผลเพี้ยน)
      · bar_date หลังแท่งสุดท้าย → PENDING (ยังไม่มีแท่ง forward)
      · pos==0 + ไม่ตรง → IMPOSSIBLE (bar_date อยู่ก่อนทั้ง window = แท่งเข้าหายจริง)
    """
    try:
        bd = pd.Timestamp(bar_date).normalize()
    except Exception:  # noqa: BLE001 — bar_date เพี้ยน → ประเมินไม่ได้
        return None, IMPOSSIBLE
    pos = int(idx.searchsorted(bd))
    if pos >= len(idx):
        return None, PENDING
    if idx[pos] == bd:
        return pos + 1, OK
    if pos == 0:
        return None, IMPOSSIBLE
    return pos, OK


def still_pending(bars_available: int, horizon: int, age_days: int, hard_expire_days: int) -> bool:
    """เก็บแท่ง forward ไม่ครบ horizon → ควรค้างไว้ประเมินรอบหน้าไหม

    ค้างไว้ = ยังไม่ปิดผล กันตัดสิน "ไม่ถึงเป้า" ทั้งที่แท่งยังไม่ออก (ช่วงวันหยุดยาว/แท่งล่าสุดยังไม่ปิด)
    ยกเว้นแก่เกิน hard_expire = feed สั้นจริง/หุ้นหยุดเทรด → ยอมปิด ไม่งั้นค้างถาวร (บทเรียนจาก a9b2287)
    """
    return bars_available < horizon and age_days < hard_expire_days


def forward_return(df: pd.DataFrame, start: int, horizon: int) -> Optional[dict]:
    """ผลตอบแทนถ้า "เข้าที่ราคาเปิดของแท่ง forward แท่งแรก" แล้วถือครบ horizon แท่ง

    ทำไมต้องเข้าที่ open ไม่ใช่ close ของแท่งสัญญาณ: รายงานถูกส่งหลังตลาดปิด
    เร็วที่สุดที่คนซื้อได้จริงคือราคาเปิดวันถัดไป — ถ้าวัดจาก close จะกิน gap เปิด
    ที่ไม่มีใครได้จริง แล้วสถิติจะสวยเกินจริงอย่างเป็นระบบ

    คืน None ถ้าแท่งไม่พอ (ให้ผู้เรียกตัดสินใจว่าจะค้างหรือปิดด้วย still_pending)
    """
    if start is None or start < 0 or start >= len(df):
        return None
    win = df.iloc[start: start + horizon]
    if len(win) < horizon:
        return None
    try:
        entry = float(win["open"].iloc[0])
        exit_px = float(win["close"].iloc[-1])
        peak = float(win["high"].astype(float).max())
        trough = float(win["low"].astype(float).min())
    except (KeyError, ValueError, TypeError):
        return None
    # ต้องเช็ก isfinite ไม่ใช่แค่ <= 0: NaN เทียบยังไงก็ False (nan <= 0 → False) จะรอดด่านไปได้
    # แล้ว ret_pct กลายเป็น nan ซึ่งพิษไปทั้งค่าเฉลี่ยของ /picks ถาวร (ปนแถวเดียวก็ nan ทั้งกระดาน)
    if not all(math.isfinite(v) for v in (entry, exit_px, peak, trough)) or entry <= 0:
        return None
    return {
        "entry": round(entry, 4),
        "ret_pct": round((exit_px / entry - 1) * 100, 2),
        "mfe_pct": round((peak / entry - 1) * 100, 2),   # กำไรสูงสุดระหว่างถือ
        "mae_pct": round((trough / entry - 1) * 100, 2),  # ขาดทุนหนักสุดระหว่างถือ
    }
