"""
scalping_bot/shadow.py — Shadow Mode: "ช่วงทดลองงาน" ของกลยุทธ์ใหม่ (paper trade)

แนวคิด (multi-strategy fund): กลยุทธ์ใหม่ห้ามแตะเงินจริงจนกว่าจะพิสูจน์ตัวเอง
  1) กลยุทธ์ใน STRATEGY_SHADOW_LIST สแกน + ผ่านทุกด่าน (รวม AI) ตามปกติ
     แต่ตอนจะยิงออเดอร์ → บันทึกลงไฟล์แทน (ไม่ส่ง MT5)
  2) ทุก journal cycle ตรวจราคาจริงย้อนหลัง (M5) ว่าสัญญาณชน SL หรือ TP ก่อน
     → ตัดสิน win/loss แบบ paper (same-bar ชนทั้งคู่ = นับแพ้ — conservative)
  3) ครบ SHADOW_MIN_TRADES → ประเมิน: ผ่าน = แจ้ง Telegram ให้เลื่อนขั้นเงินจริง
     ตก = แจ้งให้ปิดทิ้ง (แจ้งครั้งเดียว ไม่ spam)

คู่กับ auto-disable (ไล่ออก) = วงจรชีวิตกลยุทธ์ครบ: ทดลองงาน → บรรจุ → ไล่ออก
บทเรียนที่มา: rvol_brk/range_mr/rsi2 ลงเงินจริงทันที เสีย ~$35 ก่อน auto-disable ตัดทัน
"""
from __future__ import annotations
import json
import logging
import os
import time

log = logging.getLogger("part2.shadow")
_FILE = os.path.join(os.path.dirname(__file__), "scalpbot_shadow.json")


def _load() -> dict:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
                d.setdefault("trades", []); d.setdefault("notified", [])
                return d
    except Exception:  # noqa: BLE001
        pass
    return {"trades": [], "notified": []}


def _save(d: dict) -> None:
    try:
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _FILE)   # atomic — crash กลางทางไฟล์ไม่เสีย
    except Exception as e:  # noqa: BLE001
        log.warning("shadow save failed: %s", e)


def shadow_set(cfg: dict) -> set:
    """กลยุทธ์ที่อยู่ช่วงทดลองงาน (source names, lowercase)"""
    return {s.strip().lower() for s in cfg.get("STRATEGY_SHADOW_LIST", "").split(",") if s.strip()}


def record(t: dict) -> None:
    """บันทึกสัญญาณ shadow — ใบสั่งที่ผ่านทุกด่านแล้วแต่ไม่ยิงจริง (เรียกแทน _auto_open)"""
    bias = t.get("bias") or {}
    if not t.get("spot") or not t.get("sl"):
        return
    d = _load()
    d["trades"].append({
        "symbol": t.get("exsym"), "direction": t.get("direction"),
        "source": (bias.get("source") or "").lower(),
        "entry": float(t["spot"]), "sl": float(t["sl"]),
        "tp": float(t["tp"]) if t.get("tp") else None,
        "rr": float(t.get("rr") or 0) or None,
        "risk_money": (t.get("sizing") or {}).get("risk_money"),
        "opened_at": int(time.time()),
        "status": "open", "r": None, "win": None, "closed_at": None,
    })
    _save(d)
    log.info("🧪 shadow: บันทึก %s %s (%s) — paper trade ไม่ยิงจริง",
             t.get("exsym"), t.get("direction"), bias.get("source"))


def _resolve_one(tr: dict, mt5) -> bool:
    """ตัดสินสัญญาณเดียวจากราคาจริง M5 หลังเวลาเข้า — คืน True ถ้าปิดได้"""
    import pandas as pd
    age_hr = (time.time() - tr["opened_at"]) / 3600.0
    bars = min(int(age_hr * 12) + 24, 4000)          # M5 = 12 แท่ง/ชม. + buffer
    df = mt5.rates(tr["symbol"], "M5", bars)
    if df is None or len(df) < 2 or "time" not in df.columns:
        return False
    ts = pd.to_datetime(df["time"]).astype("int64") // 10**9
    after = df[ts > tr["opened_at"]]
    if after.empty:
        return False
    buy = tr["direction"] == "buy"
    sl, tp = tr["sl"], tr.get("tp")
    for _, b in after.iterrows():
        hi, lo = float(b["high"]), float(b["low"])
        hit_sl = (lo <= sl) if buy else (hi >= sl)
        hit_tp = tp is not None and ((hi >= tp) if buy else (lo <= tp))
        if hit_sl:                       # same-bar ชนทั้ง SL+TP → นับแพ้ (conservative)
            tr.update(status="closed", win=False, r=-1.0, closed_at=int(time.time()))
            return True
        if hit_tp:
            tr.update(status="closed", win=True, r=round(tr.get("rr") or 1.0, 2),
                      closed_at=int(time.time()))
            return True
    return False


def _expire_one(tr: dict, mt5, max_age_hr: float) -> bool:
    """สัญญาณค้างเกินอายุ → ปิดที่ราคาปัจจุบัน (คิด R ตามระยะจริง)"""
    if (time.time() - tr["opened_at"]) / 3600.0 < max_age_hr:
        return False
    px = mt5.price(tr["symbol"])
    if not px:
        return False
    last = px["bid"] if tr["direction"] == "buy" else px["ask"]
    dist = abs(tr["entry"] - tr["sl"])
    if dist <= 0:
        return False
    move = (last - tr["entry"]) if tr["direction"] == "buy" else (tr["entry"] - last)
    r = round(move / dist, 2)
    tr.update(status="closed", win=r > 0, r=r, closed_at=int(time.time()))
    return True


def stats_by_source() -> dict:
    """{source: {n, win, avg_r, open}} จากสัญญาณ shadow ทั้งหมด"""
    out: dict = {}
    for tr in _load()["trades"]:
        s = out.setdefault(tr["source"], {"n": 0, "win": 0, "rs": [], "open": 0})
        if tr["status"] == "open":
            s["open"] += 1
            continue
        s["n"] += 1
        s["win"] += bool(tr["win"])
        if tr["r"] is not None:
            s["rs"].append(tr["r"])
    for s in out.values():
        s["win_rate"] = round(s["win"] / s["n"] * 100) if s["n"] else 0
        s["avg_r"] = round(sum(s["rs"]) / len(s["rs"]), 2) if s["rs"] else None
        del s["rs"]
    return out


def report_text(cfg: dict) -> str:
    """ข้อความสำหรับคำสั่ง /shadow"""
    active = shadow_set(cfg)
    st = stats_by_source()
    min_n = int(cfg.get("SHADOW_MIN_TRADES", "20"))
    if not active and not st:
        return ("🧪 Shadow Mode ว่างอยู่ — ยังไม่มีกลยุทธ์ทดลองงาน\n"
                "เพิ่มด้วย: /set STRATEGY_SHADOW_LIST=ชื่อกลยุทธ์ (เช่น rsi2,range_mr)\n"
                "อย่าลืมเปิด USE_ ของกลยุทธ์นั้นด้วย — มันจะสแกนแต่ไม่ยิงเงินจริง")
    lines = [f"🧪 Shadow Mode — ทดลองงาน (เกณฑ์ {min_n} สัญญาณ)"]
    for src in sorted(active | set(st)):
        s = st.get(src)
        tag = "" if src in active else " (ออกจาก list แล้ว)"
        if not s or (s["n"] == 0 and s["open"] == 0):
            lines.append(f"▸ {src}{tag}: ยังไม่มีสัญญาณ")
            continue
        r_txt = f" · เฉลี่ย {s['avg_r']:+.2f}R" if s["avg_r"] is not None else ""
        lines.append(f"▸ {src}{tag}: ปิดแล้ว {s['n']}/{min_n} · ชนะ {s['win_rate']}%{r_txt}"
                     f" · ค้าง {s['open']}")
    return "\n".join(lines)


def process(mt5, cfg: dict, token: str, chat: str, tg) -> None:
    """รันทุก journal cycle: ตัดสินสัญญาณค้าง + ประเมินเลื่อนขั้น/ตกทดลองงาน"""
    d = _load()
    opens = [tr for tr in d["trades"] if tr["status"] == "open"]
    if not opens and not d["trades"]:
        return
    max_age = float(cfg.get("SHADOW_MAX_AGE_HR", "72"))
    changed = 0
    for tr in opens:
        try:
            if _resolve_one(tr, mt5) or _expire_one(tr, mt5, max_age):
                changed += 1
                log.info("🧪 shadow: ปิด %s %s (%s) → %s %.2fR", tr["symbol"], tr["direction"],
                         tr["source"], "ชนะ" if tr["win"] else "แพ้", tr["r"])
        except Exception as e:  # noqa: BLE001
            log.warning("shadow resolve %s fail: %s", tr.get("symbol"), e)
    if changed:
        _save(d)

    # ── ประเมินเลื่อนขั้น/ตก (แจ้งครั้งเดียวต่อกลยุทธ์) ─────────────────
    min_n = int(cfg.get("SHADOW_MIN_TRADES", "20"))
    pass_wr = float(cfg.get("SHADOW_PROMOTE_WINRATE", "45"))
    for src, s in stats_by_source().items():
        if s["n"] < min_n or src in d["notified"]:
            continue
        promote = s["win_rate"] >= pass_wr and (s["avg_r"] or 0) > 0
        if promote:
            msg = (f"🎓 Shadow: '{src}' ผ่านทดลองงาน!\n"
                   f"ปิดแล้ว {s['n']} สัญญาณ · ชนะ {s['win_rate']}% · เฉลี่ย {s['avg_r']:+.2f}R\n"
                   f"เลื่อนขั้นเป็นเงินจริง: เอา {src} ออกจาก /set STRATEGY_SHADOW_LIST=...")
        else:
            msg = (f"📉 Shadow: '{src}' ตกทดลองงาน\n"
                   f"ปิดแล้ว {s['n']} สัญญาณ · ชนะ {s['win_rate']}% (เกณฑ์ {pass_wr:.0f}%)"
                   f" · เฉลี่ย {(s['avg_r'] or 0):+.2f}R\n"
                   f"แนะนำปิดทิ้ง: /set USE_{src.upper()}=false — ไม่เสียเงินจริงสักบาท ✅")
        try:
            tg.send_text(token, chat, msg)
        except Exception:  # noqa: BLE001
            pass
        d["notified"].append(src)
        _save(d)
        log.info("🧪 shadow: ประเมิน %s → %s", src, "ผ่าน" if promote else "ตก")
