"""
Offline test: ด่านใหม่ของ /top5 — spike (ขาขึ้นหลอก) · พื้นฐานเติบโต · correlation

เคสจริงที่เป็นที่มา:
  · หุ้นได้สัญญาใหญ่ (RAMP-case) พุ่งวันเดียว → อินดิเคเตอร์บอกขาขึ้นทั้งที่ไม่มีเทรนด์จริง
  · HCSG ป้าย GICS = อุตสาหกรรม แต่ราคาวิ่งตาม healthcare → ป้ายหลอกได้ ราคาไม่หลอก

วิธีรัน:  python tests\\test_growth_filters.py
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

from core import ranking, correlate  # noqa: E402
from data.fundamentals import growth_verdict, _norm_gate_metric  # noqa: E402

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


class S:
    def __init__(self, **kw):
        self.max_day_up_pct = None
        self.ret20_pct = None
        self.dollar_vol_m = None
        self.__dict__.update(kw)


print("[1] spike_flags — จับขาขึ้นหลอก (RAMP-case)")
ok_("พุ่งวันเดียว +25% → ธง", bool(ranking.spike_flags(S(max_day_up_pct=25.0, ret20_pct=28.0))))
ok_("กำไรเดือน +12% มาจากวันเดียว +10% → ธง",
    bool(ranking.spike_flags(S(max_day_up_pct=10.0, ret20_pct=12.0))))
check("เทรนด์แท้ (ขึ้นเดือน +15% วันแรงสุด +4%) → ไม่ธง",
      ranking.spike_flags(S(max_day_up_pct=4.0, ret20_pct=15.0)), [])
check("ข้อมูลขาด → ไม่ธง (ไม่ลงโทษข้อมูลขาด)", ranking.spike_flags(S()), [])
ok_("สภาพคล่องต่ำ ($2M/วัน) → ธง", bool(ranking.spike_flags(S(dollar_vol_m=2.0))))
check("สภาพคล่องพอ ($8M/วัน) → ไม่ธง", ranking.spike_flags(S(dollar_vol_m=8.0)), [])
ok_("วันแรงสุด +9% แต่เดือนขึ้น +30% (เทรนด์จริง) → ไม่ธง",
    not ranking.spike_flags(S(max_day_up_pct=9.0, ret20_pct=30.0)))

print("\n[2] growth_verdict — ขนาด + เติบโต (ตามที่ผู้ใช้เลือก: $300M-$100B · ขาดทุนได้ถ้าโต ≥20%)")
check("mega cap $150B → fail", growth_verdict({"mcap_m": 150_000})[0], "fail")
check("เล็กเกิน $200M → fail", growth_verdict({"mcap_m": 200})[0], "fail")
check("กลาง $5B กำไรดี → pass", growth_verdict({"mcap_m": 5000, "net_margin": 0.15})[0], "pass")
check("ขาดทุนแต่รายได้โต +45% → pass",
      growth_verdict({"mcap_m": 2000, "net_margin": -0.1, "rev_g": 45})[0], "pass")
check("ขาดทุน + โตแค่ +8% → fail",
      growth_verdict({"mcap_m": 2000, "net_margin": -0.1, "rev_g": 8})[0], "fail")
check("ไม่มีข้อมูลเลย → unknown (ปล่อยผ่าน)", growth_verdict({})[0], "unknown")
check("รู้แค่ขนาด (ผ่าน) งบไม่มี → unknown", growth_verdict({"mcap_m": 5000})[0], "unknown")
check("กำไรบาง ๆ 2% ก็นับว่ามีกำไร → pass",
      growth_verdict({"mcap_m": 800, "net_margin": 0.02, "rev_g": 5})[0], "pass")
# ปรับเกณฑ์ผ่าน env ได้
check("เกณฑ์โต 30%: โต 25% → fail",
      growth_verdict({"mcap_m": 2000, "net_margin": -0.1, "rev_g": 25}, min_rev_g=30)[0], "fail")

print("\n[2b] _norm_gate_metric — หน่วยของ Finnhub ≠ FMP (จุดที่เพี้ยน 100 เท่าได้)")
m = _norm_gate_metric({"marketCapitalization": 5200.0,       # ล้าน$ → ใช้ตรง ๆ
                       "revenueGrowthTTMYoy": 34.2,          # % → ใช้ตรง ๆ
                       "netProfitMarginTTM": 12.3})          # % → ต้องหาร 100
check("mcap ล้าน$ ผ่านตรง", m.get("mcap_m"), 5200.0)
check("rev_g % ผ่านตรง", m.get("rev_g"), 34.2)
check("margin % → สัดส่วน (12.3 → 0.123)", m.get("net_margin"), 0.123)
v = growth_verdict(m)
ok_("ต่อท่อเข้า growth_verdict แล้ว margin โชว์ 12% ไม่ใช่ 1230%",
    v[0] == "pass" and "margin 12%" in v[1], f"{v}")
check("margin ติดลบ (-4.5%) เครื่องหมายไม่เพี้ยน",
      _norm_gate_metric({"netProfitMarginTTM": -4.5}).get("net_margin"), -0.045)
check("field หาย → dict ว่าง (อย่า cache)", _norm_gate_metric({}), {})
check("margin เป็นสตริงขยะ → ข้าม field นั้น",
      _norm_gate_metric({"netProfitMarginTTM": "N/A"}), {})

print("\n[3] correlate — วัดจากราคาจริง (กันป้ายเซกเตอร์หลอกแบบ HCSG)")
rng = np.random.default_rng(7)
days = pd.date_range("2026-04-01", periods=80, freq="B")
base = rng.normal(0, 0.02, 80)                      # ปัจจัยร่วมของ "กลุ่ม"
mk = lambda noise, w=1.0: pd.DataFrame(              # noqa: E731
    {"close": 100 * np.cumprod(1 + w * base + rng.normal(0, noise, 80))}, index=days)
a = mk(0.004)          # วิ่งตามกลุ่มแนบ
b = mk(0.004)          # วิ่งตามกลุ่มแนบ (คนละ noise แต่ปัจจัยร่วมเดียวกัน)
c = pd.DataFrame({"close": 100 * np.cumprod(1 + rng.normal(0, 0.02, 80))}, index=days)  # อิสระ

cab, cac = correlate.corr(a, b), correlate.corr(a, c)
ok_("คู่ที่วิ่งด้วยกัน corr สูง", cab is not None and cab > 0.8, f"corr={cab:.2f}")
ok_("คู่อิสระ corr ต่ำ", cac is not None and abs(cac) < 0.5, f"corr={cac:.2f}")

items = {"A": a, "B": b, "C": c}
check("B ถูกจับว่าวิ่งตาม A", correlate.too_correlated("B", ["A"], items, 0.75), "A")
check("C อิสระ → ผ่าน", correlate.too_correlated("C", ["A", "B"], items, 0.75), None)
check("df หาย → ตัดสินไม่ได้ → ผ่าน", correlate.too_correlated("X", ["A"], items, 0.75), None)
short = pd.DataFrame({"close": [100.0, 101.0, 99.0]},
                     index=pd.date_range("2026-07-01", periods=3, freq="B"))
check("แท่งไม่พอ → ตัดสินไม่ได้ → ผ่าน",
      correlate.too_correlated("S", ["A"], {**items, "S": short}, 0.75), None)

print()
if _FAIL:
    print(f"❌ FAIL {len(_FAIL)} เคส: {', '.join(_FAIL)}")
    sys.exit(1)
print("✅ PASS — spike (RAMP-case) · growth_verdict · correlation (HCSG-case)")
