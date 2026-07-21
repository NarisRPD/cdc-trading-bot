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
import math
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_FILE = "picks_log.json"
_HORIZONS = (5, 10, 20)
# ~120 วันทำการ × 5 ตัว/วัน — เผื่อกว้างพอที่ prune จะไม่มีวันตัดแถวที่ยังไม่ครบ 20 แท่งทิ้ง
_MAX_ROWS = 600
# แท่ง forward ไม่มาเกินเท่านี้ = feed สั้นจริง/หุ้นหยุดเทรด → ปิดแถว ไม่ค้างถาวร
_HARD_EXPIRE_DAYS = 60
# df ต้องยาวอย่างน้อยเท่านี้ถึงจะเชื่อว่า "แท่งเข้าหลุด window จริง" (รอบสแกนดึงมา 2 ปี ≈ 500 แท่ง)
_TRUST_MIN_BARS = 250
# ยิง fetch_history เดี่ยวได้กี่ตัวต่อรอบ — กันรอบสแกนยืดยาวตอน yfinance มีปัญหา
_MAX_FETCH_FALLBACK = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> Optional[list]:
    """คืน list ที่โหลดได้ · คืน None เมื่อ "โหลดไม่สำเร็จ"

    ต้องแยก 2 กรณีนี้ให้ขาด: ถ้าคืน [] ตอน GCS ล่ม แล้ว log_picks ไป append + save
    ประวัติทั้งไฟล์จะถูกเขียนทับหายถาวร (blob ไม่มี versioning) — ผู้เรียกต้องเช็ก None เสมอ
    """
    try:
        from watchlist import store
        rows = store.load_json(_FILE, [])
        return rows if isinstance(rows, list) else []
    except Exception as e:  # noqa: BLE001
        log.warning("picks: โหลดไม่สำเร็จ: %s", e)
        return None


def _rows() -> list:
    """สำหรับฝั่งอ่านอย่างเดียว (รายงาน) — โหลดพังก็คืนว่างได้ ไม่มีการเขียนทับ"""
    return _load() or []


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
    snap_bd = str(snap_bar_date)[:10] if snap_bar_date else None
    if not snap_bd:
        log.warning("picks: ไม่มี bar_date ของรอบสแกน — ข้าม")
        return 0
    try:
        rows = _load()
        if rows is None:                      # โหลดไม่สำเร็จ — ห้ามเขียน ไม่งั้นทับประวัติหมด
            log.warning("picks: อ่านไฟล์เดิมไม่ได้ — ไม่บันทึกรอบนี้ (กันเขียนทับประวัติ)")
            return 0
        # dedup ระดับ "รอบสแกน" ไม่ใช่ระดับ symbol: ถ้าแท่งนี้บันทึกไปแล้วให้ข้ามทั้งชุด
        # (ถ้า dedup ราย symbol อย่างเดียว การสแกนแท่งเดิมซ้ำที่ได้ Top N ต่างออกไป
        #  จะแทรกแถวใหม่ที่มี rank ซ้ำกับของเดิม → ranks_at()/สถิติอันดับเพี้ยน)
        if any(r.get("snap_bar_date") == snap_bd for r in rows):
            return 0
        seen = {r.get("id") for r in rows}
        added = 0
        for rank, p in enumerate(picks, 1):
            sym = p.get("symbol")
            bd = p.get("bar_date") or snap_bd
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
                # bar_date = แท่งของหุ้นตัวนั้นเอง (ใช้กับหน้าต่าง forward 5/10/20 แท่ง)
                # snap_bar_date = แท่งของ "รอบสแกน" (ใช้จัดกลุ่มว่ารอบไหนแนะนำอะไร)
                # ต้องแยกกัน เพราะหุ้นที่ feed ค้างติดอันดับได้ด้วยแท่งเก่าถึง 8 วัน
                "bar_date": bd,
                "snap_bar_date": snap_bd,
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
            log.info("picks: บันทึกคำแนะนำใหม่ %d ตัว (แท่ง %s)", added, snap_bd)
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
        if not rows:                      # None (โหลดพัง) หรือว่างจริง — ทั้งคู่ไม่มีอะไรให้ทำ
            return 0
        today = pd.Timestamp.utcnow().tz_localize(None).normalize()
        pending = [r for r in rows if not r.get("closed")][:max_eval]
        if not pending:
            return 0

        # ถ้ารอบสแกนดึงข้อมูลมาได้น้อยผิดปกติ (yfinance กำลังมีปัญหา) อย่าไปไล่ยิงทีละตัวซ้ำเติม
        items = items or {}
        allow_fetch = len(items) >= 100
        misses = 0
        cache: dict = {}
        touched = 0
        for r in pending:
            sym = r.get("symbol")
            try:
                if sym in cache:
                    df = cache[sym]
                else:
                    df = items.get(sym)
                    if (df is None or getattr(df, "empty", True)) and allow_fetch and misses < _MAX_FETCH_FALLBACK:
                        misses += 1        # หลุด universe แล้ว — ยอมยิงเดี่ยว แต่จำกัดจำนวนต่อรอบ
                        from data.quote import fetch_history
                        df = fetch_history("us", sym)
                    cache[sym] = df
                if df is None or df.empty or "open" not in df.columns:
                    continue

                age = int((today - pd.Timestamp(r["bar_date"]).normalize()).days)
                idx = forward.normalized_index(df)
                start, status = forward.forward_start(idx, r["bar_date"])
                if status == forward.IMPOSSIBLE:
                    # แท่งเข้าอยู่ก่อน window ของ feed — แต่ปิดแถวทันทีไม่ได้:
                    # บางรอบ yfinance คืน df สั้นผิดปกติ (ฝั่ง batch ไม่มีด่านความยาวขั้นต่ำ)
                    # ถ้าปิดเลย แถวจะหลุดจากการติดตามถาวรทั้งที่รอบหน้าข้อมูลกลับมาครบ
                    # → ปิดเมื่อ df ยาวพอจะเชื่อได้ หรือแก่เกิน hard-expire (กันค้างถาวร)
                    if len(df) >= _TRUST_MIN_BARS or age >= _HARD_EXPIRE_DAYS:
                        r["closed"] = True
                        r["evaluated_at"] = _now_iso()
                        touched += 1
                    continue
                if status != forward.OK:
                    if age >= _HARD_EXPIRE_DAYS:            # แท่ง forward ไม่มาเสียที — เลิกรอ
                        r["closed"] = True
                        r["evaluated_at"] = _now_iso()
                        touched += 1
                    continue                                # ยังไม่มีแท่ง forward — รอรอบหน้า

                if r.get("entry_open") is None and start < len(df):
                    _op = float(df["open"].iloc[start])
                    if math.isfinite(_op):                  # NaN ห้ามเก็บ — จะพิษไปทั้งสถิติ
                        r["entry_open"] = round(_op, 4)

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
    rows = _rows()
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

    # อันดับต้นดีกว่าอันดับท้ายจริงไหม = คะแนนใช้ได้จริงหรือเปล่า
    # ต้องใช้ horizon เดียวทั้งกลุ่ม ห้าม fallback ไป 5 แท่งเมื่อ 10 แท่งยังไม่ครบ
    # (ผล 5 แท่งเล็กกว่าโดยธรรมชาติ ปนแล้วค่าเฉลี่ยจะต่ำกว่าจริงและเทียบข้ามวันไม่ได้)
    for h in (10, 5):
        top = [v for r in rows if (v := _ret(r, h)) is not None and (r.get("rank") or 9) <= 2]
        rest = [v for r in rows if (v := _ret(r, h)) is not None and (r.get("rank") or 9) > 2]
        if top and rest:
            L.append(f"\n📊 อันดับใช้ได้จริงไหม (ถือ {h} แท่ง)\n"
                     f"   อันดับ 1-2: เฉลี่ย {sum(top)/len(top):+.1f}% (n={len(top)})\n"
                     f"   อันดับ 3 ลงไป: เฉลี่ย {sum(rest)/len(rest):+.1f}% (n={len(rest)})")
            break

    L.append("\n─────────\n"
             "วัด 'คำแนะนำ Top N' โดยเข้าที่ราคาเปิดแท่งถัดไป แล้วถือครบจำนวนแท่ง (ไม่มี SL/TP)\n"
             "ต่างจาก /calib ที่วัดสัญญาณด้วยกรอบ ±ATR · และ /stats ที่วัดไม้ที่คุณปิดเอง")
    return "\n".join(L)


def recent_bars(n: int = 2) -> list:
    """แท่งของ 'รอบสแกน' ที่เคยบันทึก ใหม่→เก่า สูงสุด n ตัว — ไว้หารอบก่อนหน้าของจริง
    (อย่าใช้ 'เมื่อวาน' ตามนาฬิกา: เสาร์อาทิตย์/วันหยุดตลาดจะเทียบผิดวัน
     และต้องใช้ snap_bar_date ไม่ใช่ bar_date รายตัว ไม่งั้นหุ้น feed ค้างตัวเดียว
     จะสร้าง 'รอบ' ปลอมขึ้นมาแล้วป้ายอันดับ ▲▼/🆕/📤 ผิดทั้งใบ)"""
    return sorted({r.get("snap_bar_date") for r in _rows() if r.get("snap_bar_date")},
                  reverse=True)[:n]


def ranks_at(snap_bar_date: str) -> dict:
    """{symbol: rank} ของรอบสแกนที่ระบุ — ไว้ diff อันดับระหว่างรอบ"""
    return {r["symbol"]: r.get("rank") for r in _rows()
            if r.get("snap_bar_date") == snap_bar_date and r.get("symbol")}


def active(limit: int = 12) -> list:
    """แถวที่ยังติดตามอยู่ (ยังไม่ปิด) ใหม่สุดก่อน — ไว้โชว์ใน /picks"""
    return [r for r in reversed(_rows()) if not r.get("closed")][:limit]
