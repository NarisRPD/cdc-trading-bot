"""
part2_mt5/learn.py — วงจรการเรียนรู้จากผลเทรดจริง (closed-loop)

แนวคิด (มือโปร ไม่ใช่ลูกเล่น): บอทจะ "เก่งขึ้น" ได้ต่อเมื่อมี *ข้อมูลผลจริง* มาวัด
  1) ตอนเปิดไม้  → บันทึก "ฟีเจอร์" ของไม้นั้น (zone/stage/RSI/แท่งเทียน/วอลุ่ม/เบรก/AI conf/ชั่วโมง)
  2) ตอนปิดไม้  → จับคู่ผลลัพธ์ (กำไร/ขาดทุน, R-multiple, แพ้/ชนะ) เข้ากับฟีเจอร์
  3) วิเคราะห์  → "เทคนิคไหนได้เงินจริง" บนโบร/สินทรัพย์ของบัญชีนี้ (edge_report)
  4) ป้อนกลับ   → สรุปบทเรียนเป็น "ความจำ" ป้อนให้ Gemini ตัดสินใจดีขึ้น (summary_for_ai)

กันหลงทาง (สำคัญ): ไม่ "เรียนรู้" จากตัวอย่างน้อย ๆ — bucket ต้องมีตัวอย่างพอ (min_bucket)
และความจำ AI จะเริ่มทำงานก็ต่อเมื่อมีไม้ปิดถึงเกณฑ์ (min_total) เท่านั้น = กัน overfitting
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("part2.learn")
_FILE = os.path.join(os.path.dirname(__file__), "part2_trades.json")
_MAGIC = 260605


def _load() -> list:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or []
    except Exception:  # noqa: BLE001
        pass
    return []


def _save(data: list) -> None:
    try:
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        log.warning("save learn failed: %s", e)


def record_entry(ticket_id, t: dict) -> None:
    """บันทึกฟีเจอร์ของไม้ตอนเปิด (เรียกหลังเปิดออเดอร์สำเร็จ) — key = position id (เลข order)"""
    try:
        pos = int(ticket_id)
    except (TypeError, ValueError):
        return
    recs = _load()
    if any(r.get("pos") == pos for r in recs):   # กันซ้ำ
        return
    bias = t.get("bias") or {}
    vol = t.get("vol") or {}
    verdict = t.get("verdict") or {}
    sizing = t.get("sizing") or {}
    snap = {
        "pos": pos,
        "symbol": t.get("exsym"),
        "direction": t.get("direction"),
        "zone": bias.get("zone"),
        "stars": bias.get("stars"),
        "stage": bias.get("stage"),
        "trend_r2": bias.get("trend_r2"),
        "rsi": bias.get("rsi"),
        "candles": [c.get("name") for c in (t.get("candles") or [])],
        "vol_entering": vol.get("entering"),
        "vol_ratio": vol.get("ratio"),
        "breakout": (t.get("breakout") or {}).get("type") if t.get("breakout") else None,
        "three_bar": (t.get("three_bar") or {}).get("detected"),
        "brt": (t.get("brt") or {}).get("detected"),
        "ibb": (t.get("ibb") or {}).get("detected"),
        "tlp": (t.get("tlp") or {}).get("detected"),
        "structure": (t.get("structure") or {}).get("label"),
        "ai_decision": verdict.get("decision"),
        "ai_conf": verdict.get("confidence"),
        "rr": t.get("rr"),
        "risk_money": sizing.get("risk_money"),
        "hour_utc": datetime.now(timezone.utc).hour,
        "opened_at": int(datetime.now(timezone.utc).timestamp()),
        "status": "open", "pnl": None, "r": None, "win": None,
    }
    recs.append(snap)
    _save(recs)
    log.info("learn: บันทึกฟีเจอร์ไม้ #%s %s %s", pos, snap["symbol"], snap["direction"])


def attach_outcomes() -> int:
    """จับคู่ผลลัพธ์ให้ไม้ที่ปิดไปแล้ว (position หายจาก positions_get) → เติม pnl/r/win คืนจำนวนที่อัปเดต"""
    import MetaTrader5 as m5
    recs = _load()
    opens = [r for r in recs if r.get("status") == "open"]
    if not opens:
        return 0
    live = {p.ticket for p in (m5.positions_get() or []) if p.magic == _MAGIC}
    n = 0
    for r in opens:
        pid = r["pos"]
        if pid in live:
            continue   # ยังเปิดอยู่ — รอ
        deals = m5.history_deals_get(position=pid)
        if not deals:
            continue
        outs = [d for d in deals if d.entry == m5.DEAL_ENTRY_OUT]
        if not outs:
            continue
        pnl = round(sum(d.profit + d.commission + d.swap for d in outs), 2)
        rm = r.get("risk_money") or 0
        r["pnl"] = pnl
        r["win"] = pnl > 0
        r["r"] = round(pnl / rm, 2) if rm and rm > 0 else None
        r["status"] = "closed"
        r["closed_at"] = max(d.time for d in outs)
        n += 1
    if n:
        _save(recs)
        log.info("learn: เติมผลลัพธ์ไม้ปิด %d", n)
    return n


# ── ตัวจัด bucket: (ชื่อฟีเจอร์, ฟังก์ชันแปลง rec → label หรือ None=ไม่เข้าข่าย) ──
def _b_volume(r):
    v = r.get("vol_entering")
    return ("วอลุ่มเข้า", "✅ เข้า" if v else "ไม่เข้า") if v is not None else None


def _b_breakout(r):
    return ("เบรกกรอบ", "🚀 เบรก" if r.get("breakout") else "ไม่เบรก")


def _b_threebar(r):
    v = r.get("three_bar")
    return ("3-Bar Play", "🔥 ใช่" if v else "ไม่") if v is not None else None


def _b_brt(r):
    v = r.get("brt")
    return ("Breakout-Retest", "🔁 ใช่" if v else "ไม่") if v is not None else None


def _b_ibb(r):
    v = r.get("ibb")
    return ("Inside-Bar BO", "📦 ใช่" if v else "ไม่") if v is not None else None


def _b_tlp(r):
    v = r.get("tlp")
    return ("2-Leg Pullback", "📐 ใช่" if v else "ไม่") if v is not None else None


def _b_rr(r):
    v = r.get("rr")
    if not isinstance(v, (int, float)):
        return None
    lab = "<1.5" if v < 1.5 else ("1.5-2" if v < 2 else ("2-3" if v < 3 else "≥3"))
    return ("R:R", lab)


def _b_dir(r):
    d = r.get("direction")
    return ("ทิศทาง", "🟢 Buy" if d == "buy" else "🔴 Sell") if d else None


def _b_stage(r):
    s = r.get("stage")
    return ("Weinstein Stage", f"Stage {s}") if s else None


def _b_aiconf(r):
    c = r.get("ai_conf")
    return ("ความมั่นใจ AI", "สูง ≥70" if c >= 70 else "ต่ำ <70") if isinstance(c, (int, float)) else None


def _b_rsi(r):
    v = r.get("rsi")
    if not isinstance(v, (int, float)):
        return None
    lab = "<30 oversold" if v < 30 else (">70 overbought" if v > 70 else "30-70 กลาง")
    return ("RSI", lab)


def _b_candle(r):
    return ("แท่งเทียนยืนยัน", "มี" if r.get("candles") else "ไม่มี")


def _b_struct(r):
    s = r.get("structure")
    return ("โครงสร้าง", s) if s else None


_BUCKETS = [_b_volume, _b_breakout, _b_threebar, _b_brt, _b_ibb, _b_tlp, _b_rr, _b_dir, _b_stage, _b_aiconf, _b_rsi, _b_candle, _b_struct]


def _closed(recs=None) -> list:
    return [r for r in (recs if recs is not None else _load()) if r.get("status") == "closed"]


def _grouped(closed: list) -> dict:
    """คืน {feature: {label: [recs...]}}"""
    out: dict = {}
    for r in closed:
        for bf in _BUCKETS:
            res = bf(r)
            if not res:
                continue
            feat, lab = res
            out.setdefault(feat, {}).setdefault(lab, []).append(r)
    return out


def _bucket_stats(rs: list) -> dict:
    n = len(rs)
    wins = sum(1 for r in rs if r.get("win"))
    rs_r = [r["r"] for r in rs if r.get("r") is not None]
    pnls = [r["pnl"] for r in rs if r.get("pnl") is not None]
    return {"n": n, "win_rate": round(wins / n * 100, 0) if n else 0,
            "avg_r": round(sum(rs_r) / len(rs_r), 2) if rs_r else None,
            "total": round(sum(pnls), 2) if pnls else 0}


def overview() -> dict:
    c = _closed()
    s = _bucket_stats(c)
    s["open"] = len([r for r in _load() if r.get("status") == "open"])
    return s


def edge_report(min_bucket: int = 3) -> str:
    """รายงาน 'เทคนิคไหนได้เงินจริง' จากผลเทรด — สำหรับคำสั่ง /insights"""
    closed = _closed()
    if len(closed) < 3:
        o = overview()
        return (f"🧠 กำลังเก็บข้อมูลเรียนรู้…\nปิดแล้ว {len(closed)} ไม้ · เปิดอยู่ {o['open']} ไม้\n"
                "ต้องมีไม้ปิด ≥3 ไม้ถึงเริ่มสรุป edge ได้ (ยิ่งมาก ยิ่งแม่น)")
    ov = _bucket_stats(closed)
    lines = [f"🧠 บทเรียนจากผลเทรดจริง (ปิดแล้ว {ov['n']} ไม้)",
             f"ภาพรวม: ชนะ {ov['win_rate']:.0f}% · เฉลี่ย "
             + (f"{ov['avg_r']:+.2f}R" if ov['avg_r'] is not None else "—")
             + f" · รวม ${ov['total']:+.2f}",
             f"— จำแนกตามเทคนิค (เฉพาะที่ตัวอย่าง ≥{min_bucket}) —"]
    grouped = _grouped(closed)
    shown = 0
    for feat, labs in grouped.items():
        stats = {lab: _bucket_stats(rs) for lab, rs in labs.items()}
        stats = {lab: s for lab, s in stats.items() if s["n"] >= min_bucket}
        if not stats:
            continue
        shown += 1
        lines.append(f"\n▸ {feat}")
        for lab, s in sorted(stats.items(), key=lambda kv: kv[1]["win_rate"], reverse=True):
            r_txt = f"{s['avg_r']:+.2f}R" if s["avg_r"] is not None else f"${s['total']:+.0f}"
            lines.append(f"   • {lab}: ชนะ {s['win_rate']:.0f}% ({r_txt}, {s['n']} ไม้)")
    if not shown:
        lines.append("\n(ยังไม่มีกลุ่มไหนตัวอย่างพอ — เทรดต่อไปให้ข้อมูลสะสม)")
    lines.append("\nℹ️ เป็นสถิติย้อนหลัง ไม่การันตีอนาคต — ตลาดเปลี่ยน edge เปลี่ยนได้")
    return "\n".join(lines)


def summary_for_ai(min_total: int = 10, min_bucket: int = 5) -> str:
    """สรุปบทเรียนสั้น ๆ ป้อนเข้า Gemini เป็น 'ความจำ' — คืน '' ถ้าข้อมูลยังน้อย (กันชี้นำผิด)"""
    closed = _closed()
    if len(closed) < min_total:
        return ""
    grouped = _grouped(closed)
    lessons = []
    for feat, labs in grouped.items():
        stats = {lab: _bucket_stats(rs) for lab, rs in labs.items() if len(rs) >= min_bucket}
        if not stats:
            continue
        # หยิบ label ที่ "ดีสุด" และ "แย่สุด" ถ้าต่างกันชัด
        best = max(stats.items(), key=lambda kv: kv[1]["win_rate"])
        worst = min(stats.items(), key=lambda kv: kv[1]["win_rate"])
        if best[0] != worst[0] and best[1]["win_rate"] - worst[1]["win_rate"] >= 15:
            lessons.append(f"{feat}: '{best[0]}' ชนะ {best[1]['win_rate']:.0f}% ดีกว่า "
                           f"'{worst[0]}' {worst[1]['win_rate']:.0f}%")
        elif worst[1]["avg_r"] is not None and worst[1]["avg_r"] < -0.1:
            lessons.append(f"{feat}: '{worst[0]}' ขาดทุนเฉลี่ย {worst[1]['avg_r']:+.2f}R (ระวัง)")
    if not lessons:
        return ""
    head = (f"บทเรียนจากสถิติจริงของบัญชีนี้ ({len(closed)} ไม้ — ใช้เป็นน้ำหนักประกอบ "
            "ไม่ใช่กฎตายตัว เพราะตลาดเปลี่ยนได้):")
    return head + " " + " · ".join(lessons[:6])
