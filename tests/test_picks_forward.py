"""
Offline regression test สำหรับ core/forward.py + watchlist/picks.py

โฟกัส 4 อย่างที่ "ผิดแล้วสถิติโกหกแบบเงียบ ๆ":
  1) เข้าที่ราคา *เปิดของแท่งถัดไป* ไม่ใช่ close ของแท่งสัญญาณ
     (ถ้าวัดจาก close จะกิน gap เปิดที่ไม่มีใครซื้อได้จริง → ผลงานสวยเกินจริง)
  2) แท่งสัญญาณไม่อยู่ใน index (วันหยุด/feed revise) ต้องไม่ข้ามแท่ง forward แรก — บั๊ก a9b2287
  3) แท่งยังไม่ครบ horizon ต้องค้าง pending ไม่ใช่ปิดผลด่วน
  4) dedup ด้วย symbol|bar_date → /scan ซ้ำกลางวันต้องไม่เกิดแถวซ้ำ

รันแบบไม่ต่อเน็ต: inject fake watchlist.store เข้า sys.modules
วิธีรัน:  python tests\\test_picks_forward.py
"""
from __future__ import annotations

import logging
import os
import sys
import types

import pandas as pd

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

_TODAY = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
_FAIL: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: got={got!r} want={want!r}")
    if not ok:
        _FAIL.append(name)


def _df(dates, opens, highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes}, index=dates)


# ─────────────────── 1) core/forward — หาแท่งเข้า ───────────────────
def test_forward_start() -> None:
    from core import forward
    print("\n[1] forward_start — หาแท่ง forward แท่งแรก")
    days = pd.date_range("2026-03-02", periods=5, freq="D")   # 02,03,04,05,06
    idx = forward.normalized_index(_df(days, [1] * 5, [1] * 5, [1] * 5, [1] * 5))

    # ตรงวัน → เริ่มแท่งถัดไป
    check("ตรงวัน 03-03 → start=2", forward.forward_start(idx, "2026-03-03"), (2, forward.OK))
    # ไม่ตรงวัน (bar_date เป็นวันหยุด) → idx[pos] คือแท่ง forward แรกอยู่แล้ว ห้าม +1
    idx2 = forward.normalized_index(_df(
        pd.DatetimeIndex(["2026-03-02", "2026-03-05", "2026-03-06"]), [1] * 3, [1] * 3, [1] * 3, [1] * 3))
    check("ไม่ตรงวัน 03-03 → start=1 (ไม่ข้ามแท่งแรก)",
          forward.forward_start(idx2, "2026-03-03"), (1, forward.OK))
    # หลังแท่งสุดท้าย → รอ
    check("หลังแท่งสุดท้าย → PENDING",
          forward.forward_start(idx, "2026-03-10"), (None, forward.PENDING))
    # ก่อนทั้ง window → ประเมินไม่ได้ตลอดกาล
    check("ก่อนทั้ง window → IMPOSSIBLE",
          forward.forward_start(idx, "2026-01-01"), (None, forward.IMPOSSIBLE))
    check("bar_date เพี้ยน → IMPOSSIBLE",
          forward.forward_start(idx, "ไม่ใช่วันที่"), (None, forward.IMPOSSIBLE))


# ─────────────── 2) core/forward — ผลตอบแทนต้องนับจาก open ───────────────
def test_forward_return() -> None:
    from core import forward
    print("\n[2] forward_return — เข้าที่ open ของแท่งถัดไป")
    days = pd.date_range("2026-03-02", periods=4, freq="D")
    # แท่ง 0 = แท่งสัญญาณ close=100 · แท่ง 1 เปิด gap ที่ 110 (คนซื้อได้จริงที่ 110 ไม่ใช่ 100)
    df = _df(days,
             opens=[100, 110, 112, 118],
             highs=[101, 115, 120, 125],
             lows=[99, 108, 111, 117],
             closes=[100, 112, 118, 121])
    r = forward.forward_return(df, 1, 3)
    check("entry ใช้ open ของแท่ง forward แรก (110) ไม่ใช่ close แท่งสัญญาณ (100)", r["entry"], 110.0)
    check("ret = close แท่งสุดท้าย 121 เทียบ 110", r["ret_pct"], round((121 / 110 - 1) * 100, 2))
    check("mfe = high สูงสุด 125 เทียบ 110", r["mfe_pct"], round((125 / 110 - 1) * 100, 2))
    check("mae = low ต่ำสุด 108 เทียบ 110", r["mae_pct"], round((108 / 110 - 1) * 100, 2))
    check("แท่งไม่พอ → None", forward.forward_return(df, 1, 5), None)
    check("start เกิน df → None", forward.forward_return(df, 99, 1), None)

    # NaN ต้องไม่รอดด่าน: `nan <= 0` เป็น False จึงกันด้วย <=0 อย่างเดียวไม่ได้
    # ถ้ารอดไปจะได้ ret_pct=nan แล้วค่าเฉลี่ยของ /picks เป็น nan ทั้งกระดานถาวร
    df_nan = _df(days,
                 opens=[100, float("nan"), 112, 118],
                 highs=[101, 115, 120, 125],
                 lows=[99, 108, 111, 117],
                 closes=[100, 112, 118, 121])
    check("open เป็น NaN → None (ไม่ปล่อย nan เข้าสถิติ)", forward.forward_return(df_nan, 1, 3), None)
    df_nan2 = _df(days, [100, 110, 112, 118], [101, 115, 120, 125],
                  [99, 108, 111, 117], [100, 112, 118, float("nan")])
    check("close เป็น NaN → None", forward.forward_return(df_nan2, 1, 3), None)
    df_zero = _df(days, [100, 0.0, 112, 118], [101, 115, 120, 125],
                  [99, 108, 111, 117], [100, 112, 118, 121])
    check("open = 0 → None", forward.forward_return(df_zero, 1, 3), None)

    print("\n[3] still_pending — ค้างไว้ vs ยอมปิด")
    check("แท่งไม่ครบ + ยังไม่แก่ → ค้าง", forward.still_pending(3, 5, 10, 60), True)
    check("แท่งไม่ครบ + แก่เกิน → ปิด", forward.still_pending(3, 5, 61, 60), False)
    check("แท่งครบ → ไม่ต้องค้าง", forward.still_pending(5, 5, 1, 60), False)


# ─────────────────── 4) watchlist/picks — บันทึก + ประเมิน ───────────────────
def _install_store(rows: list) -> None:
    store = types.ModuleType("watchlist.store")
    store.load_json = lambda name, default=None: rows      # คืน list เดิม (mutate in place)
    store.save_json = lambda name, data: None
    store.get_by_ticker = lambda t: []
    sys.modules["watchlist.store"] = store


def test_picks() -> None:
    rows: list = []
    _install_store(rows)
    for m in ("watchlist.picks",):
        sys.modules.pop(m, None)
    from watchlist import picks

    print("\n[4] log_picks — dedup ด้วย symbol|bar_date")
    # วันที่ต้องสัมพัทธ์กับวันนี้เสมอ: picks.evaluate ตัดสิน pending/expired จาก "อายุแถว"
    # ถ้าใช้วันที่ตายตัว เทสจะค่อย ๆ เพี้ยนเมื่อเวลาผ่านไปจนเกิน hard-expire 60 วัน
    bd_ts = _TODAY - pd.Timedelta(days=8)
    bd = bd_ts.strftime("%Y-%m-%d")
    bd_next = (bd_ts + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    p = [{"symbol": "AAA", "bar_date": bd, "score": 80, "price": 100.0},
         {"symbol": "BBB", "bar_date": bd, "score": 70, "price": 50.0}]
    check("บันทึกครั้งแรก 2 ตัว", picks.log_picks(p, bd), 2)
    check("บันทึกซ้ำแท่งเดิม → 0 แถวใหม่", picks.log_picks(p, bd), 0)
    check("อันดับถูกเก็บตามลำดับที่ส่งเข้า", [r["rank"] for r in rows], [1, 2])
    check("bar_date รายตัวถูกใช้", rows[0]["bar_date"], bd)
    p2 = [{"symbol": "AAA", "bar_date": bd_next, "score": 82, "price": 104.0}]
    check("แท่งใหม่ของ symbol เดิม → เพิ่มได้", picks.log_picks(p2, bd_next), 1)
    check("recent_bars ใหม่→เก่า", picks.recent_bars(2), [bd_next, bd])
    check("ranks_at", picks.ranks_at(bd), {"AAA": 1, "BBB": 2})

    # สแกนแท่งเดิมซ้ำแล้วได้ Top N คนละหน้า → ต้องข้ามทั้งชุด ไม่ใช่แทรกแถวที่ rank ซ้ำ
    check("สแกนแท่งเดิมซ้ำแต่ชุดต่างออกไป → ไม่เพิ่มแถว",
          picks.log_picks([{"symbol": "ZZZ", "bar_date": bd, "score": 90, "price": 9.0}], bd), 0)
    check("ranks_at ยังไม่มี rank ซ้ำ", sorted(picks.ranks_at(bd).values()), [1, 2])

    # หุ้น feed ค้าง: bar_date รายตัวเก่ากว่าแท่งของรอบ → ต้องไม่สร้าง 'รอบ' ปลอม
    bd3 = (bd_ts + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    stale_bd = (bd_ts - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    picks.log_picks([{"symbol": "AAA", "bar_date": bd3, "score": 80, "price": 100.0},
                     {"symbol": "OLD", "bar_date": stale_bd, "score": 79, "price": 12.0}], bd3)
    check("recent_bars อิงแท่งของรอบสแกน ไม่ใช่ bar_date รายตัว",
          picks.recent_bars(3), [bd3, bd_next, bd])
    check("หุ้น feed ค้างยังถูกจัดอยู่ในรอบที่ถูกต้อง", picks.ranks_at(bd3), {"AAA": 1, "OLD": 2})

    print("\n[4b] log_picks — โหลดไฟล์เดิมไม่ได้ ห้ามเขียนทับประวัติ")
    import watchlist.store as _st
    _orig = _st.load_json
    _st.load_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("GCS 503"))
    saved = []
    _orig_save = _st.save_json
    _st.save_json = lambda name, data: saved.append(data)
    check("โหลดพัง → คืน 0 และไม่เรียก save", picks.log_picks(p, bd_next), 0)
    check("ไม่มีการเขียนไฟล์เลย", len(saved), 0)
    _st.load_json, _st.save_json = _orig, _orig_save

    print("\n[5] evaluate — เติมผลตามจำนวนแท่งที่มีจริง")
    # หลังแท่งสัญญาณมี 6 แท่ง → r5 ได้ · r10/r20 ต้องค้าง (แท่งไม่พอ แต่ยังไม่แก่เกิน 60 วัน)
    days = pd.date_range(bd, periods=7, freq="D")
    df_aaa = _df(days,
                 opens=[100, 100, 101, 102, 103, 104, 105],
                 highs=[101, 103, 104, 105, 106, 107, 108],
                 lows=[99, 99, 100, 101, 102, 103, 104],
                 closes=[100, 102, 103, 104, 105, 110, 111])
    rows_before = len(rows)
    picks.evaluate(cfg=None, items={"AAA": df_aaa, "BBB": df_aaa})
    aaa = [r for r in rows if r["id"] == f"AAA|{bd}"][0]
    check("entry_open = open ของแท่งถัดจากแท่งสัญญาณ (100)", aaa["entry_open"], 100.0)
    check("r5 คำนวณแล้ว", isinstance(aaa.get("r5"), dict), True)
    check("r5 ret = close แท่งที่ 5 (110) เทียบ entry 100", aaa["r5"]["ret_pct"], 10.0)
    check("r10 ยังค้าง (แท่งไม่พอ + ยังไม่แก่)", aaa.get("r10"), None)
    check("ยังไม่ปิดแถว", aaa.get("closed"), False)
    check("จำนวนแถวไม่เปลี่ยนจากการ evaluate", len(rows), rows_before)

    print("\n[6] evaluate — แท่งเข้าหลุด window: ปิดเฉพาะเมื่อ df ยาวพอจะเชื่อได้")
    # df สั้น (yfinance คืนไม่ครบชั่วคราว) → ห้ามปิดแถว ไม่งั้นหลุดการติดตามถาวร
    short = pd.date_range(_TODAY - pd.Timedelta(days=1), periods=3, freq="D")
    df_short = _df(short, [10] * 3, [11] * 3, [9] * 3, [10] * 3)
    picks.evaluate(cfg=None, items={"AAA": df_short, "BBB": df_short})
    bbb = [r for r in rows if r["id"] == f"BBB|{bd}"][0]
    check("df สั้น → ยังไม่ปิด (รอข้อมูลกลับมาครบ)", bbb.get("closed"), False)

    # df ยาวจริง (≥250 แท่ง) แล้วยังไม่ครอบ bar_date → แท่งเข้าหลุด window จริง ปิดได้
    long_idx = pd.date_range(_TODAY - pd.Timedelta(days=1), periods=260, freq="D")
    df_long = _df(long_idx, [10] * 260, [11] * 260, [9] * 260, [10] * 260)
    picks.evaluate(cfg=None, items={"AAA": df_long, "BBB": df_long})
    check("df ยาวพอ + แท่งเข้าหลุด window → closed", bbb.get("closed"), True)

    print("\n[7] summary — ไม่ล่มและมีตัวเลข")
    s = picks.summary()
    check("summary มีบรรทัดผล 5 แท่ง", "ถือ 5 แท่ง" in s, True)


if __name__ == "__main__":
    test_forward_start()
    test_forward_return()
    test_picks()
    print()
    if _FAIL:
        print(f"❌ FAIL {len(_FAIL)} เคส: {', '.join(_FAIL)}")
        sys.exit(1)
    print("✅ PASS — forward (เข้าที่ open/ไม่ข้ามแท่งแรก/pending) + picks (dedup/ประเมิน/ปิดแถว)")
