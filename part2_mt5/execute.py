"""
part2_mt5/execute.py — เปิดออเดอร์บน MT5 (market order + SL/TP)

แนบ SL/TP ไปกับออเดอร์ → MT5 จะปิดให้เองเมื่อถึง TP หรือ SL (ไม่ต้องเฝ้า)
*** ยิงจริงเฉพาะเมื่อ EXECUTE_ORDERS=true · แนะนำเริ่มที่บัญชี Demo ***
"""
from __future__ import annotations
import logging

log = logging.getLogger("part2.execute")
_MAGIC = 260605  # ป้ายระบุว่าเป็นออเดอร์ของ Part 2


def place_order(exsym: str, direction: str, lots: float, sl: float, tp: float,
                deviation: int = 30) -> dict:
    """เปิด market order พร้อม SL/TP → คืน {ok, retcode, comment, ticket, price}"""
    import MetaTrader5 as m5
    m5.symbol_select(exsym, True)
    info = m5.symbol_info(exsym)
    tick = m5.symbol_info_tick(exsym)
    if info is None or tick is None:
        return {"ok": False, "comment": "ไม่พบสัญลักษณ์/ราคา"}
    is_buy = direction == "buy"
    price = tick.ask if is_buy else tick.bid
    order_type = m5.ORDER_TYPE_BUY if is_buy else m5.ORDER_TYPE_SELL

    base = {
        "action": m5.TRADE_ACTION_DEAL, "symbol": exsym, "volume": float(lots),
        "type": order_type, "price": price, "sl": float(sl), "tp": float(tp),
        "deviation": deviation, "magic": _MAGIC, "comment": "Part2-Bot",
        "type_time": m5.ORDER_TIME_GTC,
    }
    # ลอง filling mode หลายแบบ (โบรกต่างกัน) — IOC → FOK → RETURN
    for fill in (m5.ORDER_FILLING_IOC, m5.ORDER_FILLING_FOK, m5.ORDER_FILLING_RETURN):
        req = {**base, "type_filling": fill}
        res = m5.order_send(req)
        if res is None:
            return {"ok": False, "comment": f"order_send None: {m5.last_error()}"}
        if res.retcode == m5.TRADE_RETCODE_DONE:
            return {"ok": True, "retcode": res.retcode, "ticket": res.order,
                    "price": res.price, "comment": "สำเร็จ"}
        # 10030 = filling mode ไม่รองรับ → ลองแบบถัดไป
        if res.retcode != 10030:
            return {"ok": False, "retcode": res.retcode, "comment": res.comment}
    return {"ok": False, "comment": "filling mode ไม่รองรับทุกแบบ"}


def modify_sltp(ticket: int, sl: float, tp: float) -> bool:
    """แก้ SL/TP ของ position (ใช้เลื่อน SL เท่าทุน/trailing)
    ⚠️ MT5 TRADE_ACTION_SLTP ต้องมี "symbol" ไม่งั้น reject เงียบ (retcode 10013)"""
    import MetaTrader5 as m5
    # ดึง symbol จาก position — จำเป็นสำหรับ TRADE_ACTION_SLTP
    pos_info = m5.positions_get(ticket=int(ticket))
    if not pos_info:
        log.warning("modify_sltp: ไม่พบ position #%d", ticket)
        return False
    sym = pos_info[0].symbol
    res = m5.order_send({
        "action":   m5.TRADE_ACTION_SLTP,
        "position": int(ticket),
        "symbol":   sym,
        "sl":       float(sl),
        "tp":       float(tp),
    })
    if res is None:
        log.warning("modify_sltp #%d %s: order_send None — %s", ticket, sym, m5.last_error())
        return False
    ok = res.retcode == m5.TRADE_RETCODE_DONE
    if not ok:
        log.warning("modify_sltp #%d %s: retcode=%d %s", ticket, sym, res.retcode, res.comment)
    return ok


def close_position(pos, volume: float = None) -> dict:
    """ปิด position (ทั้งหมดถ้า volume=None หรือบางส่วนถ้าระบุ) — market opposite"""
    import MetaTrader5 as m5
    # ใช้ `is not None` ไม่ใช่ `if volume` — volume=0.0 เป็น falsy แต่หมายความต่างกัน
    vol = float(volume) if volume is not None else float(pos.volume)
    is_buy = pos.type == m5.POSITION_TYPE_BUY
    tick = m5.symbol_info_tick(pos.symbol)
    if tick is None:
        return {"ok": False, "comment": "ไม่มีราคา"}
    price = tick.bid if is_buy else tick.ask          # ปิด Buy ที่ bid · ปิด Sell ที่ ask
    otype = m5.ORDER_TYPE_SELL if is_buy else m5.ORDER_TYPE_BUY
    base = {"action": m5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": vol,
            "type": otype, "position": int(pos.ticket), "price": price,
            "deviation": 30, "magic": _MAGIC, "comment": "Part2-Bot-close"}
    for fill in (m5.ORDER_FILLING_IOC, m5.ORDER_FILLING_FOK, m5.ORDER_FILLING_RETURN):
        res = m5.order_send({**base, "type_filling": fill})
        if res is None:
            return {"ok": False, "comment": f"None: {m5.last_error()}"}
        if res.retcode == m5.TRADE_RETCODE_DONE:
            return {"ok": True, "comment": "ปิดแล้ว"}
        if res.retcode != 10030:
            return {"ok": False, "retcode": res.retcode, "comment": res.comment}
    return {"ok": False, "comment": "filling ไม่รองรับ"}
