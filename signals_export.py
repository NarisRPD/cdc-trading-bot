"""
signals_export.py — สะพาน Part 1 → Part 2 (MT5)

ปล่อยสัญญาณ CDC ล่าสุดเป็น JSON ลง GCS (signals_latest.json) ให้ Part 2 ไปหยิบ
*** ไม่กระทบ logic เดิมของ Part 1 — เป็น output เสริมล้วน ๆ (พังก็แค่ข้าม ไม่ทำสแกนล่ม) ***

Part 1 = วิเคราะห์/ให้สัญญาณ · Part 2 = เอาสัญญาณไปประกอบการเทรด (sizing/SL/TP/แท่งเทียน)
→ ที่นี่ส่งเฉพาะ "สัญญาณ + ตัวเลขเทคนิคดิบ" ไม่ตัดสินใจเทรดแทน Part 2
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_FILE = "signals_latest.json"
_MARKET_OF = {"Crypto": "crypto", "US Stocks": "us", "Thai Stocks": "thai", "Commodities": "commodity"}


def _sig_to_dict(s, market: str, section: str, direction: str) -> dict:
    st = s.stage or {}
    tq = s.trend_q or {}
    return {
        "symbol": s.symbol,
        "display": s.display_name,
        "market": market,
        "section": section,          # cdc_buy / cdc_sell / reversal_up / reversal_down
        "direction": direction,      # buy / sell (ทิศที่จะเทรด)
        "close": s.close,
        "stars": s.score,
        "high_quality": bool(s.high_quality),
        "rs_rank": s.rs_rank,
        "atr": s.atr,
        "adx": s.adx,
        "rsi": s.rsi,
        "ema_fast": s.ema_fast,       # EMA12 — Part 2 ใช้คิดแนวรับ/เข้า
        "ema_slow": s.ema_slow,       # EMA26
        "zone": s.zone,
        "stage": st.get("n"),
        "stage_label": st.get("label"),
        "trend_r2": tq.get("r2"),     # คุณภาพเทรนด์ (เนียน/ขรุขระ)
        "bar_date": s.bar_date.strftime("%Y-%m-%d") if s.bar_date is not None else None,
    }


def export_signals(results, cfg) -> int:
    """เขียนสัญญาณทุกกลุ่มลง GCS — เรียกจาก main() (ครอบ try/except กันพังสแกน)
    ใช้ตัวกรองเดียวกับที่โชว์จริง (HQ + sideway) เพื่อให้ Part 2 เห็นชุดเดียวกับที่ผู้ใช้เห็น"""
    try:
        from watchlist import store
        from main import _filter_for_alert, _is_sideway
    except Exception as e:  # noqa: BLE001
        log.warning("export_signals import ไม่ได้: %s", e)
        return 0
    out: list[dict] = []
    sw = cfg.sideway_adx_max if cfg.filter_sideway else 0.0
    for r in results:
        mk = _MARKET_OF.get(r.group_name, "")
        for s in _filter_for_alert(r.buy, cfg):
            out.append(_sig_to_dict(s, mk, "cdc_buy", "buy"))
        for s in _filter_for_alert(r.sell, cfg):
            out.append(_sig_to_dict(s, mk, "cdc_sell", "sell"))
        for s in r.reversal:  # ใกล้กลับตัว (กรอง sideway เหมือนที่โชว์)
            if s.zone == "blue" and not _is_sideway(s, sw):
                out.append(_sig_to_dict(s, mk, "reversal_up", "buy"))
            elif s.zone == "orange" and not _is_sideway(s, sw):
                out.append(_sig_to_dict(s, mk, "reversal_down", "sell"))
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(out),
        "signals": out,
    }
    try:
        store.save_json(_FILE, payload)
        log.info("export_signals: เขียน %d สัญญาณ → %s", len(out), _FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("export_signals เขียนไม่สำเร็จ: %s", e)
    return len(out)
