"""
Offline regression test สำหรับ signals_log.evaluate_outcomes()

โฟกัส: บั๊ก "expired ด่วน" — สัญญาณที่เก็บแท่ง forward ไม่ครบ window (วันหยุดเยอะ /
แท่งล่าสุดยังไม่ออก) ต้อง "ค้างไว้ (pending)" ไม่ใช่ปิดเป็น expired ทันที
เว้นแต่แก่เกิน hard_expire (feed สั้นจริง) → ยอมปิด expired กันค้างถาวร

รันแบบไม่ต่อเน็ต: inject fake watchlist.store + data.quote เข้า sys.modules
ใช้ pandas จริง (ตรรกะ searchsorted/slicing เป็นหัวใจที่ต้องทดสอบของจริง)

วิธีรัน:  .venv\\Scripts\\python.exe tests\\test_evaluate_outcomes.py
"""
from __future__ import annotations

import logging
import os
import sys
import types

import pandas as pd

# คอนโซล Windows (cp1252) พิมพ์ไทยไม่ได้ → บังคับ UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

# ให้ import signals_log / watchlist / data จาก repo root ได้ (ไฟล์นี้อยู่ใน tests/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

_TODAY = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)


def _bar_date(days_ago: int) -> str:
    return (_TODAY - pd.Timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _make_df(bar_date_str: str, forward: list[tuple[float, float]]) -> pd.DataFrame:
    """แท่งสัญญาณ (pos 0, ค่าไม่สำคัญ) + แท่ง forward (high, low) ต่อท้าย"""
    start = pd.Timestamp(bar_date_str)
    dates = pd.date_range(start=start, periods=1 + len(forward), freq="D")
    highs = [100.0] + [h for h, _ in forward]
    lows = [100.0] + [low for _, low in forward]
    closes = [100.0] * (1 + len(forward))
    return pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=dates)


def _make_df_missing(bar_date_str: str, forward: list[tuple[float, float]]) -> pd.DataFrame:
    """index ที่ "ไม่มีแท่ง bar_date" (วันสัญญาณเป็นวันหยุด/โดน revise ทิ้ง) แต่มีแท่งก่อนหน้า
    → searchsorted(bd) ชี้ pos>0 ที่ idx[pos] != bd = แท่ง forward แรกจริง
    forward = แท่งหลัง bd (high, low)"""
    bd = pd.Timestamp(bar_date_str)
    prior = pd.date_range(end=bd - pd.Timedelta(days=1), periods=2, freq="D")   # 2 แท่งก่อน bd
    after = pd.date_range(start=bd + pd.Timedelta(days=1), periods=len(forward), freq="D")
    dates = prior.append(after)
    highs = [100.0, 100.0] + [h for h, _ in forward]
    lows = [100.0, 100.0] + [low for _, low in forward]
    closes = [100.0] * len(dates)
    return pd.DataFrame({"high": highs, "low": lows, "close": closes}, index=dates)


def _row(sym: str, direction: str, bar_date_str: str) -> dict:
    return {
        "id": f"{sym}|{bar_date_str}|{direction}",
        "outcome": None, "atr": 2.0, "bar_date": bar_date_str,
        "market": "us", "symbol": sym, "close": 100.0, "direction": direction,
    }


def _install_fakes(rows: list[dict], df_by_symbol: dict[str, pd.DataFrame]) -> None:
    store = types.ModuleType("watchlist.store")
    store.load_json = lambda name, default=None: rows          # คืน list เดิม (mutate in place)
    store.save_json = lambda name, data: None
    sys.modules["watchlist.store"] = store

    quote = types.ModuleType("data.quote")
    quote.fetch_history = lambda market, ticker, crypto_exchange="binance", **kw: df_by_symbol.get(ticker)
    sys.modules["data.quote"] = quote


def main() -> int:
    # กันค่า env แปลกปลอมจากเครื่อง dev (ใช้ default 1.5 / 1.5 / 10 / hard=30)
    for k in ("EVAL_TARGET_ATR", "EVAL_STOP_ATR", "EVAL_LOOKAHEAD_BARS", "EVAL_HARD_EXPIRE_DAYS"):
        os.environ.pop(k, None)

    no_hit = (101.0, 99.0)  # ไม่ชนทั้ง target(103)/stop(97) ของ buy, และ target(97)/stop(103) ของ sell

    # ── สร้างเคส (entry=100, atr=2 → buy tgt 103/stp 97 · sell tgt 97/stp 103) ──
    bd16 = _bar_date(16)   # ครบอายุประเมิน แต่ยังไม่แก่ (< hard_expire 30)
    bd79 = _bar_date(79)   # แก่เกิน hard_expire

    cases = {
        # A) buy, window ไม่ครบ (8<10), ไม่ชนอะไร, ยังไม่แก่ → ต้อง pending (outcome None)
        "PEND": (_row("PEND", "buy", bd16), [no_hit] * 8, None),
        # B) buy, window ครบ 10, ชน target แท่งที่ 9 → win (bars 9)
        "WINB": (_row("WINB", "buy", bd16),
                 [no_hit] * 8 + [(104.0, 99.0), (101.0, 99.0)], "win"),
        # C) buy, window ครบ 10, ไม่ชนอะไร → expired
        "EXPF": (_row("EXPF", "buy", bd16), [no_hit] * 10, "expired"),
        # D) buy, ชน stop แท่งที่ 2 → loss
        "LOSS": (_row("LOSS", "buy", bd16),
                 [no_hit, (101.0, 96.0)] + [no_hit] * 8, "loss"),
        # E) buy, window ไม่ครบ (8<10) แต่แก่เกิน hard_expire → ยอมปิด expired
        "HARD": (_row("HARD", "buy", bd79), [no_hit] * 8, "expired"),
        # F) sell, ชน target (low<=97) แท่งที่ 3 → win
        "SELL": (_row("SELL", "sell", bd16),
                 [no_hit, (101.0, 98.0), (101.0, 96.0)] + [no_hit] * 7, "win"),
        # G) buy, bar_date ไม่อยู่ใน index (วันหยุด) → forward แท่งแรกชน target → win (bars 1)
        #    พิสูจน์ fix searchsorted: ถ้าใช้ pos+1 เดิมจะข้ามแท่งนี้ → กลายเป็น expired/pending ผิด
        "MISS": (_row("MISS", "buy", bd16), [(104.0, 99.0)] + [no_hit] * 9, "win"),
        # H) buy, ชนเป้าเร็ว (bar 1) แล้วราคาตกแรงทีหลัง (−6ATR) → MFE/MAE ต้องวัดเฉพาะช่วงถือ
        #    mae ต้อง 0.5 (ไม่ใช่ 6.0 จากแท่งหลังไม้ปิด) → พิสูจน์ fix holding-period
        "HOLD": (_row("HOLD", "buy", bd16),
                 [(104.0, 99.0)] + [no_hit] * 4 + [(101.0, 88.0)] + [no_hit] * 4, "win"),
    }

    _missing = {"MISS"}  # ใช้ df แบบ "ไม่มีแท่ง bar_date" เฉพาะเคสนี้
    rows = [c[0] for c in cases.values()]
    df_by_symbol = {
        sym: (_make_df_missing if sym in _missing else _make_df)(c[0]["bar_date"], c[1])
        for sym, c in cases.items()
    }
    _install_fakes(rows, df_by_symbol)

    # import หลัง install fakes (signals_log import store/quote แบบ lazy ในฟังก์ชันอยู่แล้ว)
    import importlib
    import signals_log
    importlib.reload(signals_log)

    cfg = types.SimpleNamespace(crypto_exchange="binance")
    evaluated = signals_log.evaluate_outcomes(cfg)

    by_id = {r["symbol"]: r for r in rows}
    failures = []

    def check(sym, want):
        got = by_id[sym].get("outcome")
        if got != want:
            failures.append(f"{sym}: outcome={got!r} ต้องเป็น {want!r}")

    for sym, (_, _, want) in cases.items():
        check(sym, want)

    # ตรวจรายละเอียดเพิ่ม (กัน off-by-one ของ bars + ค่า mfe/mae)
    if by_id["WINB"].get("outcome") == "win":
        if by_id["WINB"].get("bars_to_outcome") != 9:
            failures.append(f"WINB bars_to_outcome={by_id['WINB'].get('bars_to_outcome')} ต้องเป็น 9")
        if by_id["WINB"].get("mfe_r") != 2.0:
            failures.append(f"WINB mfe_r={by_id['WINB'].get('mfe_r')} ต้องเป็น 2.0")
        if by_id["WINB"].get("mae_r") != 0.5:
            failures.append(f"WINB mae_r={by_id['WINB'].get('mae_r')} ต้องเป็น 0.5")
    if by_id["LOSS"].get("outcome") == "loss" and by_id["LOSS"].get("bars_to_outcome") != 2:
        failures.append(f"LOSS bars_to_outcome={by_id['LOSS'].get('bars_to_outcome')} ต้องเป็น 2")
    if by_id["SELL"].get("outcome") == "win" and by_id["SELL"].get("bars_to_outcome") != 3:
        failures.append(f"SELL bars_to_outcome={by_id['SELL'].get('bars_to_outcome')} ต้องเป็น 3")
    # MISS: forward แท่งแรกชน → bars ต้อง 1 (ถ้า fix searchsorted พลาดจะข้ามแท่ง = ไม่ win/bars เพี้ยน)
    if by_id["MISS"].get("outcome") == "win" and by_id["MISS"].get("bars_to_outcome") != 1:
        failures.append(f"MISS bars_to_outcome={by_id['MISS'].get('bars_to_outcome')} ต้องเป็น 1 (searchsorted)")
    # HOLD: ชนเป้า bar 1 → mae ต้องวัดเฉพาะช่วงถือ = 0.5 (ไม่ใช่ 6.0 จากแท่ง −6ATR หลังไม้ปิด)
    if by_id["HOLD"].get("outcome") == "win":
        if by_id["HOLD"].get("bars_to_outcome") != 1:
            failures.append(f"HOLD bars_to_outcome={by_id['HOLD'].get('bars_to_outcome')} ต้องเป็น 1")
        if by_id["HOLD"].get("mae_r") != 0.5:
            failures.append(f"HOLD mae_r={by_id['HOLD'].get('mae_r')} ต้องเป็น 0.5 (วัดเฉพาะช่วงถือ)")
        if by_id["HOLD"].get("mfe_r") != 2.0:
            failures.append(f"HOLD mfe_r={by_id['HOLD'].get('mfe_r')} ต้องเป็น 2.0")

    # PEND ต้องยังไม่ถูกประเมิน → ไม่นับใน evaluated; finalized = 7 (B,C,D,E,F,G,H)
    if evaluated != 7:
        failures.append(f"evaluated={evaluated} ต้องเป็น 7 (PEND ค้างไว้)")
    if by_id["PEND"].get("evaluated_at") is not None:
        failures.append("PEND ไม่ควรมี evaluated_at (ต้องค้าง pending)")

    print("\n=== ผลแต่ละเคส ===")
    for sym, (_, _, want) in cases.items():
        r = by_id[sym]
        print(f"  {sym:5s} want={want!s:8s} got={str(r.get('outcome')):8s} "
              f"bars={r.get('bars_to_outcome')} mfe={r.get('mfe_r')} mae={r.get('mae_r')}")
    print(f"evaluated(finalized)={evaluated}")

    if failures:
        print("\n❌ FAIL:")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✅ PASS — ครบทุกเคส (pending/win/expired/loss/hard-expire/sell)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
