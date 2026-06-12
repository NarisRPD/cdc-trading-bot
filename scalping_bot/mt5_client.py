"""
scalping_bot/mt5_client.py — ตัวต่อ MT5 terminal

broker-agnostic: เชื่อม terminal ที่ "ล็อกอินไว้แล้ว" (ปลอดภัย ไม่ต้องเก็บรหัสผ่านในโค้ด)
อ่าน: บัญชี · สัญลักษณ์ที่โบรกมี · สเปกสัญญา · ราคา · OHLC · คำนวณ lot แม่นจากสเปกจริง
"""
from __future__ import annotations
import logging
from typing import Optional

log = logging.getLogger("part2.mt5")

_TF = {}  # เติมตอน import MetaTrader5 (lazy กันเครื่องที่ไม่มี)


def _mt5():
    import MetaTrader5 as mt5
    if not _TF:
        _TF.update({"M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
                    "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1})
    return mt5


def connect(path: Optional[str] = None, login: Optional[int] = None,
            password: Optional[str] = None, server: Optional[str] = None) -> bool:
    """เชื่อม MT5 — ถ้า terminal เปิด+ล็อกอินอยู่แล้ว เรียก connect() เปล่าก็พอ
    (ใส่ login/password/server เฉพาะถ้าต้องการให้ initialize ล็อกอินเอง)"""
    mt5 = _mt5()
    kw: dict = {}
    if path:
        kw["path"] = path
    if login:
        kw.update(login=int(login), password=password or "", server=server or "")
    ok = mt5.initialize(**kw)
    if not ok:
        log.error("MT5 initialize ล้มเหลว: %s", mt5.last_error())
    return bool(ok)


def shutdown() -> None:
    try:
        _mt5().shutdown()
    except Exception:  # noqa: BLE001
        pass


def account() -> Optional[dict]:
    a = _mt5().account_info()
    if a is None:
        return None
    return {"login": a.login, "name": a.name, "balance": a.balance, "equity": a.equity,
            "currency": a.currency, "leverage": a.leverage, "server": a.server, "margin_free": a.margin_free}


def list_symbols() -> list[str]:
    syms = _mt5().symbols_get()
    return [s.name for s in syms] if syms else []


def find_symbol(query: str) -> list[str]:
    """หา symbol ที่มี query อยู่ในชื่อ (เช่น 'XAU'→XAUUSD, 'AAPL'→AAPL/#AAPL/AAPL.us)"""
    q = query.upper().lstrip("$").replace(".BK", "")
    return [n for n in list_symbols() if q in n.upper()]


def symbol_spec(sym: str) -> Optional[dict]:
    mt5 = _mt5()
    mt5.symbol_select(sym, True)  # ให้แน่ใจว่า symbol ถูกเปิดใน Market Watch
    s = mt5.symbol_info(sym)
    if s is None:
        return None
    return {"name": s.name, "point": s.point, "digits": s.digits,
            "contract_size": s.trade_contract_size, "tick_value": s.trade_tick_value,
            "tick_size": s.trade_tick_size, "volume_min": s.volume_min,
            "volume_max": s.volume_max, "volume_step": s.volume_step,
            "currency_profit": s.currency_profit}


def price(sym: str) -> Optional[dict]:
    t = _mt5().symbol_info_tick(sym)
    if t is None:
        return None
    return {"bid": t.bid, "ask": t.ask, "time": t.time}


def rates(sym: str, timeframe: str = "H1", n: int = 300):
    """OHLC ล่าสุด n แท่ง → DataFrame [time,open,high,low,close,volume] (None ถ้าไม่มี)"""
    import pandas as pd
    mt5 = _mt5()
    mt5.symbol_select(sym, True)
    tf = _TF.get(timeframe.upper(), mt5.TIMEFRAME_H1)
    r = mt5.copy_rates_from_pos(sym, tf, 0, n)
    if r is None or len(r) == 0:
        return None
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.rename(columns={"tick_volume": "volume"})
    return df[["time", "open", "high", "low", "close", "volume"]]


def lots_for_risk(sym: str, balance: float, risk_pct: float,
                  entry: float, sl: float) -> Optional[dict]:
    """คำนวณ lot ให้ขาดทุนที่ SL ≈ balance×risk% — ใช้ tick_value/tick_size จริงของโบรก
    คืน {lots, target_risk, risk_money(จริงหลังปัด lot), actual_pct, loss_per_lot, dist}
    *** risk_money = ความเสี่ยง 'จริง' หลังปัดเป็น lot ขั้นต่ำ (อาจสูงกว่า target ถ้าพอร์ตเล็ก) ***"""
    spec = symbol_spec(sym)
    if not spec:
        return None
    dist = abs(entry - sl)
    ts, tv = spec["tick_size"], spec["tick_value"]
    if dist <= 0 or ts <= 0 or tv <= 0:
        return None
    target_risk = balance * risk_pct / 100.0
    loss_per_lot = (dist / ts) * tv               # ขาดทุนต่อ 1.0 lot ถ้าโดน SL
    if loss_per_lot <= 0:
        return None
    step = spec["volume_step"] or 0.01
    lots = round((target_risk / loss_per_lot) / step) * step
    lots = max(spec["volume_min"], min(lots, spec["volume_max"]))
    lots = round(lots, 2)
    actual_risk = lots * loss_per_lot             # ความเสี่ยงจริงหลังปัด lot
    actual_pct = (actual_risk / balance * 100.0) if balance > 0 else None
    return {"lots": lots, "target_risk": round(target_risk, 2),
            "risk_money": round(actual_risk, 2),
            "actual_pct": round(actual_pct, 2) if actual_pct is not None else None,
            "loss_per_lot": round(loss_per_lot, 2), "dist": dist}
