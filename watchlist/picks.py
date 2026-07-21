"""
watchlist/picks.py — ติดตามผลของ "คำแนะนำ Top N" (forward-test ของ /top5)

ต่างจากของที่มีอยู่แล้ว:
  · journal.py     = ไม้ที่ผู้ใช้เปิด/ปิดเอง  → สะท้อนวินัยผู้ใช้ (selection bias)
  · signals_log.py = สัญญาณ buy/sell ทุกตัวที่โชว์ → ตัดสินด้วย barrier ±ATR ใน 10 แท่ง
  · picks.py (นี่)  = เฉพาะหุ้นที่ "ติดอันดับ Top N" → วัด forward return 5/10/20 แท่ง
                     ตอบคำถามเดียว: "อันดับที่บอทแนะนำ ถือแล้วได้เงินจริงไหม และอันดับ 1 ดีกว่าอันดับ 5 ไหม"

กติกาสำคัญ (มาจากการรีวิว — อย่าแก้โดยไม่อ่านเหตุผล):
  1) เข้าที่ "ราคาเปิดของแท่งถัดไป" ไม่ใช่ close ของแท่งสัญญาณ — รายงานส่งหลังตลาดปิด
     เร็วสุดที่ซื้อได้จริงคือเปิดวันถัดไป ถ้าวัดจาก close จะกิน gap ที่ไม่มีใครได้ = สถิติสวยเกินจริง
  2) นับเป็น "แท่ง" ไม่ใช่ "รอบสแกน" — สแกนอาจล่ม/ผู้ใช้สั่งเองกลางวัน จำนวนรอบ ≠ จำนวนวัน
  3) dedup ด้วย symbol|bar_date ของ Signal ตัวนั้นเอง (ไม่ใช่วันที่ตามนาฬิกา) → first-writer-wins
  4) ไฟล์นี้เขียนโดย "job สแกน" เท่านั้น — store ไม่มี lock ถ้าหลาย job เขียนพร้อมกันจะ lost update
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_FILE = "picks_log.json"
_HORIZONS = (5, 10, 20)
# ~120 วันทำการ × 5 ตัว/วัน — เผื่อกว้างพอที่ prune จะไม่มีวันตัดแถวที่ยังไม่ครบ 20 แท่งทิ้ง
_MAX_ROWS = 600
# แท่ง forward ไม่มาเกินเท่านี้ = feed สั้นจริง/หุ้นหยุดเทรด → ปิดแถว ไม่ค้างถาวร
_HARD_EXPIRE_DAYS = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list:
    try:
        from watchlist import store
        rows = store.load_json(_FILE, [])
        return rows if isinstance(rows, list) else []
    except Exception as e:  # noqa: BLE001
        log.warning("picks: โหลดไม่สำเร็จ: %s", e)
        return []


def _save(rows: list) -> None:
    from watchlist import store
    store.save_json(_FILE, rows[-_MAX_ROWS:])


# ─────────────────────────── บันทึก ───────────────────────────
def log_picks(picks: list, snap_bar_date=None) -> int:
    """เก็บหุ้นที่ติดอันดับรอบนี้ — เรียกจาก run_us_stocks (ข้อมูลครบมือแล้ว ไม่ต้อง fetch)

    idempotent: ถ้ามี symbol|bar_date นั้นแล้วข้าม → ผู้ใช้สั่ง /scan usstocks ซ้ำกลางวัน
    ก็ไม่เกิดแถวซ้ำ และอันดับที่บันทึกไว้คือของรอบแรกของแท่งนั้นเสมอ
    คืนจำนวนแถวที่เพิ่มใหม่
    """
    if not picks:
        return 0
    try:
        rows = _load()
        seen = {r.get("id") for r in rows}
        added = 0
        for rank, p in enumerate(picks, 1):
            sym = p.get("symbol")
            bd = p.get("bar_date") or snap_bar_date
            if not sym or not bd:
                continue
            bd = str(bd)[:10]
            rid = f"{sym}|{bd}"
            if rid in seen:
                continue
            seen.add(rid)
            rows.append({
                "id": rid,
                "symbol": sym,
                "bar_date": bd,
                "logged_at": _now_iso(),
                "rank": rank,
                "score": p.get("score"),
                "entry_close": p.get("price"),      # ไว้ debug เท่านั้น — สถิติใช้ entry_open
                "zone": p.get("zone"),
                "stage": p.get("stage"),
                "rs_rank": p.get("rs_rank"),
                "setup_score": p.get("setup_score"),
                "reasons": p.get("reasons") or [],
                "late_at_entry": p.get("late") or [],
                "entry_open": None,
                "r5": None, "r10": None, "r20": None,
                "closed": False,
                "evaluated_at": None,
            })
            added += 1
        if added:
            _save(rows)
            log.info("picks: บันทึกคำแนะนำใหม่ %d ตัว (แท่ง %s)", added, str(snap_bar_date)[:10])
        return added
    except Exception as e:  # noqa: BLE001 — ห้ามทำให้สแกนล่ม
        log.warning("picks: บันทึกไม่สำเร็จ: %s", e)
        return 0


# ─────────────────────────── ประเมินผล ───────────────────────────
def evaluate(cfg, items: Optional[dict] = None, max_eval: int = 150) -> int:
    """เติมผล 5/10/20 แท่งให้แถวที่ยังไม่ปิด — เรียกจาก run_us_stocks พร้อมส่ง items เข้ามา

    items = dict ticker→DataFrame ที่รอบสแกนดึงมาแล้ว (~1,100 ตัว)
    → ใช้ของในมือก่อน ยิง yfinance เพิ่มเฉพาะตัวที่หลุด universe = แทบไม่กิน quota เลย
    คืนจำนวนแถวที่ได้ผลเพิ่มรอบนี้
    """
    try:
        import pandas as pd
        from core import forward
        rows = _load()
        if not rows:
            return 0
        today = pd.Timestamp.utcnow().tz_localize(None).normalize()
        pending = [r for r in rows if not r.get("closed")][:max_eval]
        if not pending:
            return 0

        cache: dict = {}
        touched = 0
        for r in pending:
            sym = r.get("symbol")
            try:
                if sym in cache:
                    df = cache[sym]
                else:
                    df = (items or {}).get(sym)
                    if df is None or getattr(df, "empty", True):
                        from data.quote import fetch_history
                        df = fetch_history("us", sym)
                    cache[sym] = df
                if df is None or df.empty or "open" not in df.columns:
                    continue

                idx = forward.normalized_index(df)
                start, status = forward.forward_start(idx, r["bar_date"])
                if status == forward.IMPOSSIBLE:
                    r["closed"] = True                      # แท่งเข้าหลุด window ของ feed — ประเมินไม่ได้ตลอดกาล
                    r["evaluated_at"] = _now_iso()
                    touched += 1
                    continue
                if status != forward.OK:
                    continue                                # ยังไม่มีแท่ง forward — รอรอบหน้า

                if r.get("entry_open") is None and start < len(df):
                    r["entry_open"] = round(float(df["open"].iloc[start]), 4)

                age = int((today - pd.Timestamp(r["bar_date"]).normalize()).days)
                avail = max(0, len(df) - start)
                got = False
                for h in _HORIZONS:
                    key = f"r{h}"
                    if r.get(key) is not None:
                        continue
                    fr = forward.forward_return(df, start, h)
                    if fr:
                        r[key] = fr
                        got = True
                    elif not forward.still_pending(avail, h, age, _HARD_EXPIRE_DAYS):
                        r[key] = "expired"                  # แท่งไม่มีวันครบแล้ว — เลิกรอ
                        got = True
                if got:
                    r["evaluated_at"] = _now_iso()
                    touched += 1
                # ปิดแถวเมื่อรู้ผลยาวสุดแล้ว หรือแก่เกินกำหนด
                if r.get("r20") is not None or age >= _HARD_EXPIRE_DAYS:
                    r["closed"] = True
            except Exception as e:  # noqa: BLE001
                log.debug("picks eval %s ข้าม: %s", r.get("id"), e)

        if touched:
            _save(rows)
            log.info("picks: ประเมินเพิ่ม %d แถว", touched)
        return touched
    except Exception as e:  # noqa: BLE001
        log.warning("picks: ประเมินไม่สำเร็จ: %s", e)
        return 0


# ─────────────────────────── รายงาน ───────────────────────────
def _ret(r: dict, h: int) -> Optional[float]:
    v = r.get(f"r{h}")
    return v.get("ret_pct") if isinstance(v, dict) else None


def summary() -> str:
    """สถิติ 'คำแนะนำแม่นแค่ไหน' — ป้อน /picks"""
    rows = _load()
    if not rows:
        return "🎯 ยังไม่มีประวัติคำแนะนำ — บอทจะเริ่มเก็บตั้งแต่รอบสแกนถัดไป"

    L = [f"🎯 ผลของคำแนะนำ Top picks (เก็บ {len(rows)} รายการ)"]
    any_done = False
    for h in _HORIZONS:
        vals = [v for v in (_ret(r, h) for r in rows) if v is not None]
        if not vals:
            continue
        any_done = True
        win = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        med = sorted(vals)[len(vals) // 2]
        L.append(f"• ถือ {h} แท่ง (n={len(vals)}): บวก {win/len(vals)*100:.0f}% · "
                 f"เฉลี่ย {avg:+.1f}% · กลาง {med:+.1f}%")

    if not any_done:
        L.append("• ยังไม่มีรายการไหนครบ 5 แท่ง — รออีกสักพัก")
        return "\n".join(L)

    # อันดับ 1-2 ดีกว่าอันดับ 3-5 จริงไหม = คะแนนใช้ได้จริงหรือเปล่า
    top, rest = [], []
    for r in rows:
        v = _ret(r, 10) if _ret(r, 10) is not None else _ret(r, 5)
        if v is None:
            continue
        (top if (r.get("rank") or 9) <= 2 else rest).append(v)
    if top and rest:
        L.append(f"\n📊 อันดับใช้ได้จริงไหม (ถือ 10 แท่ง)\n"
                 f"   อันดับ 1-2: เฉลี่ย {sum(top)/len(top):+.1f}% (n={len(top)})\n"
                 f"   อันดับ 3-5: เฉลี่ย {sum(rest)/len(rest):+.1f}% (n={len(rest)})")

    L.append("\n─────────\n"
             "วัด 'คำแนะนำ Top N' โดยเข้าที่ราคาเปิดแท่งถัดไป แล้วถือครบจำนวนแท่ง (ไม่มี SL/TP)\n"
             "ต่างจาก /calib ที่วัดสัญญาณด้วยกรอบ ±ATR · และ /stats ที่วัดไม้ที่คุณปิดเอง")
    return "\n".join(L)


def recent_bars(n: int = 2) -> list:
    """bar_date ที่เคยบันทึกไว้ ใหม่→เก่า สูงสุด n ตัว — ไว้หา 'รอบก่อนหน้า' ของจริง
    (อย่าใช้ 'เมื่อวาน' ตามนาฬิกา: เสาร์อาทิตย์/วันหยุดตลาดจะเทียบผิดวัน)"""
    return sorted({r.get("bar_date") for r in _load() if r.get("bar_date")}, reverse=True)[:n]


def ranks_at(bar_date: str) -> dict:
    """{symbol: rank} ของแท่งที่ระบุ — ไว้ diff อันดับระหว่างรอบ"""
    return {r["symbol"]: r.get("rank") for r in _load()
            if r.get("bar_date") == bar_date and r.get("symbol")}


def active(limit: int = 12) -> list:
    """แถวที่ยังติดตามอยู่ (ยังไม่ปิด) ใหม่สุดก่อน — ไว้โชว์ใน /picks"""
    return [r for r in reversed(_load()) if not r.get("closed")][:limit]
