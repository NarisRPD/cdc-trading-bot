"""
watchlist/journal.py — สมุดบันทึกไม้ที่ปิดแล้ว + สถิติ
บันทึกตอน /sell|/callsell|/putsell แล้วคำนวณ win-rate / ค่าเฉลี่ย R / expectancy
เก็บใน journal.json (list ของ trade)
"""
from __future__ import annotations
from typing import Optional

from watchlist import store

_FILE = "journal.json"


def record_trade(
    pos: dict,
    exit_price: Optional[float],
    pnl_pct: Optional[float],
    closed_at: str,
) -> dict:
    """
    บันทึก 1 ไม้ที่ปิด — คำนวณ R-multiple = กำไร% ÷ ระยะ SL เดิม%
    คืน trade record ที่บันทึก
    """
    risk_pct = pos.get("risk_pct")  # ระยะ SL เดิมเป็น % (ความเสี่ยงเริ่มต้น)
    r_mult = None
    if pnl_pct is not None and risk_pct:
        r_mult = round(pnl_pct / risk_pct, 2)

    trade = {
        "symbol": pos.get("symbol"),
        "display": pos.get("display"),
        "side": pos.get("side"),
        "entry_price": pos.get("entry_price"),
        "exit_price": exit_price,
        "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
        "r_multiple": r_mult,
        "opened_at": pos.get("entry_time"),
        "closed_at": closed_at,
        # D3: เก็บ feature ตอนเข้า (เดิม make_position คำนวณแล้วทิ้ง) → feedback loop "เข้าโซนไหน/ADX เท่าไหร่ win สูง"
        "entry_zone": pos.get("entry_zone"),
        "entry_adx": pos.get("adx"),
        "market": pos.get("market"),
    }
    trades = store.load_json(_FILE, [])
    trades.append(trade)
    store.save_json(_FILE, trades)
    return trade


def list_trades() -> list[dict]:
    return store.load_json(_FILE, [])


def compute_stats() -> Optional[dict]:
    """คำนวณสถิติจาก journal — คืน None ถ้ายังไม่มีไม้ปิด"""
    trades = list_trades()
    if not trades:
        return None

    rmults = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    pnls = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
    wins = [p for p in pnls if p > 0]

    n = len(trades)
    win_rate = (len(wins) / len(pnls) * 100.0) if pnls else None
    avg_r = (sum(rmults) / len(rmults)) if rmults else None
    total_r = sum(rmults) if rmults else None

    # D3: group-by โซนที่เข้า → win-rate ต่อโซน (feedback loop)
    by_zone: dict = {}
    for t in trades:
        z, p = t.get("entry_zone"), t.get("pnl_pct")
        if z and p is not None:
            by_zone.setdefault(z, []).append(p)
    zone_stats = {
        z: {"n": len(ps), "win_rate": round(sum(1 for x in ps if x > 0) / len(ps) * 100.0)}
        for z, ps in by_zone.items()
    }

    return {
        "trades": n,
        "win_rate": round(win_rate, 1) if win_rate is not None else None,
        "avg_r": round(avg_r, 2) if avg_r is not None else None,      # = expectancy ต่อไม้
        "total_r": round(total_r, 2) if total_r is not None else None,
        "best_r": round(max(rmults), 2) if rmults else None,
        "worst_r": round(min(rmults), 2) if rmults else None,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 2) if pnls else None,
        "by_zone": zone_stats,
    }
