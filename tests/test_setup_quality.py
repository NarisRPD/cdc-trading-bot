"""
Offline unit test สำหรับ core.signals.setup_quality()

ครอบทุกปัจจัย (buy + sell บางส่วน) + penalty + เคสฐานไม่ติดอะไร + เช็ก no-repaint
รันไม่ต่อเน็ต (pure function บน DataFrame ที่ประกอบเอง · pandas/numpy จริง)

วิธีรัน:  .venv\\Scripts\\python.exe tests\\test_setup_quality.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.signals import setup_quality  # noqa: E402

ATR = 1.0


def _df(close, *, high=None, low=None, open_=None, vol=None) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    n = len(close)
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": close.copy() if open_ is None else np.asarray(open_, float),
        "high": close + 0.5 if high is None else np.asarray(high, float),
        "low": close - 0.5 if low is None else np.asarray(low, float),
        "close": close,
        "volume": np.full(n, 1000.0) if vol is None else np.asarray(vol, float),
    }, index=idx)


def _keys(res) -> dict:
    """{key: delta} ของปัจจัยที่ติด"""
    if not res:
        return {}
    return {f["key"]: f["delta"] for f in res["factors"]}


def main() -> int:
    fails: list[str] = []

    def expect(name, res, key, delta=None, *, score=None):
        ks = _keys(res)
        if key is not None and key not in ks:
            fails.append(f"{name}: ไม่พบปัจจัย '{key}' (ได้ {ks})")
        elif key is not None and delta is not None and ks.get(key) != delta:
            fails.append(f"{name}: '{key}' delta={ks.get(key)} ต้องเป็น {delta}")
        if score is not None and res is not None and res["score"] != score:
            fails.append(f"{name}: score={res['score']} ต้องเป็น {score}")

    base = [100.0] * 60  # ฐานแบน high=100.5/low=99.5/range=1.0 (<1.5 ATR) → ไม่ติดอะไร

    # 1) BREAKOUT buy — ปิดทะลุ High 20 วัน (เฉพาะแท่งล่าสุด)
    c = list(base); c[-1] = 100.6
    h = [x + 0.5 for x in c]; h[-1] = 100.7
    r = setup_quality(_df(c, high=h), True, atr_val=ATR)
    expect("breakout_buy", r, "breakout", +1)

    # 2) BREAKOUT sell — ปิดหลุด Low 20 วัน
    c = list(base); c[-1] = 99.4
    low = [x - 0.5 for x in c]; low[-1] = 99.3
    r = setup_quality(_df(c, low=low), False, atr_val=ATR)
    expect("breakout_sell", r, "breakout", +1)

    # 3) GAP buy — เปิดข้าม High แท่งก่อน ≥0.3 ATR แล้วยืน
    c = list(base); c[-1] = 101.2
    op = list(base); op[-1] = 101.0           # prior high=100.5 → gap +0.5
    h = [x + 0.5 for x in c]; h[-1] = 101.5
    r = setup_quality(_df(c, open_=op, high=h), True, atr_val=ATR)
    expect("gap_buy", r, "gap", +1)

    # 4) LONG CANDLE buy — ช่วงกว้าง ≥1.5 ATR ปิดใกล้ปลายบน + เขียว
    c = list(base); c[-1] = 101.8
    h = [x + 0.5 for x in c]; h[-1] = 102.0
    low = [x - 0.5 for x in c]; low[-1] = 99.5     # range = 2.5 ≥ 1.5
    op = list(base); op[-1] = 100.0                # close>open = เขียว
    r = setup_quality(_df(c, high=h, low=low, open_=op), True, atr_val=ATR)
    expect("long_candle_buy", r, "long_candle", +1)

    # 5) STRUCTURE buy — HH-HL (ramp ขึ้นทั้งสูงและต่ำ)
    c = list(np.linspace(96.0, 104.0, 60))
    r = setup_quality(_df(c), True, atr_val=ATR)
    expect("structure_buy", r, "structure", +1)

    # 6) VOLUME PRESSURE buy + (accumulation: วันขึ้น volume สูง)
    c = list(base); v = [1000.0] * 60
    for j in range(35, 60):
        if (j - 35) % 2 == 0:
            c[j] = 100.3; v[j] = 2000.0   # up + vol สูง = สะสม
        else:
            c[j] = 100.0; v[j] = 500.0
    r = setup_quality(_df(c, vol=v), True, atr_val=ATR)
    expect("vol_pressure_buy_pos", r, "volume_pressure", +1)

    # 7) VOLUME PRESSURE buy − (distribution: วันลง volume สูง → หักคะแนน)
    c = list(base); v = [1000.0] * 60
    for j in range(35, 60):
        if (j - 35) % 2 == 0:
            c[j] = 99.7; v[j] = 2000.0    # down + vol สูง = แจกของ
        else:
            c[j] = 100.0; v[j] = 500.0
    r = setup_quality(_df(c, vol=v), True, atr_val=ATR)
    expect("vol_pressure_buy_neg", r, "volume_pressure", -1, score=-1)

    # 8) TRENDLINE buy — เส้นต่ำชันขึ้น
    c = list(np.linspace(95.0, 105.0, 60))
    r = setup_quality(_df(c), True, atr_val=ATR)
    expect("trendline_buy", r, "trendline", +1)

    # 9) OVEREXTENDED penalty — ยืดไกล EMA12 ≥3 ATR
    r = setup_quality(_df(base), True, atr_val=ATR, ext_atr=3.5)
    expect("overextended", r, "overextended", -1, score=-1)

    # 10) MESSY penalty — R² ต่ำ
    r = setup_quality(_df(base), True, atr_val=ATR, trend_q_r2=0.2)
    expect("messy", r, "messy", -1, score=-1)

    # 11) BASE — ไม่ติดอะไร score 0
    r = setup_quality(_df(base), True, atr_val=ATR)
    expect("base_none", r, None, score=0)

    # 12) NO-REPAINT — แท่งล่าสุด high พุ่ง 200 แต่ "close" ไม่ทะลุ → breakout ต้องไม่ติด
    #     (ยืนยันว่า breakout ดูที่ close เทียบแท่งก่อนหน้า ไม่ได้แอบใช้ high ของแท่งตัวเอง)
    c = list(base)  # close ทุกแท่ง = 100 (ไม่ทะลุ)
    h = [x + 0.5 for x in c]; h[-1] = 200.0
    r = setup_quality(_df(c, high=h), True, atr_val=ATR)
    if "breakout" in _keys(r):
        fails.append("no_repaint: breakout ไม่ควรติด (close ไม่ทะลุ แม้ high แท่งตัวเองพุ่ง)")

    # 13) ข้อมูลไม่พอ → None
    if setup_quality(_df([100.0] * 20), True, atr_val=ATR) is not None:
        fails.append("short_data: ข้อมูล <43 แท่ง ต้องคืน None")

    # 14) NaN-aware breakout — breakout จริง แต่มี NaN ใน high ในหน้าต่าง 20 วัน → ต้องยังติด (nanmax)
    c = list(base); c[-1] = 100.6
    h = [x + 0.5 for x in c]; h[-1] = 100.7; h[-10] = float("nan")
    r = setup_quality(_df(c, high=h), True, atr_val=ATR)
    expect("breakout_nan_window", r, "breakout", +1)

    # 15) NaN-aware structure — ramp ขึ้น + NaN ใน high ครึ่งแรก → ต้องยังติด
    c = list(np.linspace(96.0, 104.0, 60))
    h = [x + 0.5 for x in c]; h[25] = float("nan")
    r = setup_quality(_df(c, high=h), True, atr_val=ATR)
    expect("structure_nan", r, "structure", +1)

    print("\n=== สรุปปัจจัยที่ตรวจ ===")
    for nm, res in [
        ("breakout_buy", setup_quality(_df([*base[:-1], 100.6], high=[*( [x+0.5 for x in base][:-1]), 100.7]), True, atr_val=ATR)),
    ]:
        print(f"  {nm}: {_keys(res)} score={res['score'] if res else None}")

    if fails:
        print("\n❌ FAIL:")
        for f in fails:
            print("  -", f)
        return 1
    print("\n✅ PASS — setup_quality ครบทุกปัจจัย + no-repaint + penalty + base")
    return 0


if __name__ == "__main__":
    sys.exit(main())
