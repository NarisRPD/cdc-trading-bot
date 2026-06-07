"""
part2_mt5/run.py — ตัวรันหลัก Part 2

วน: ต่อ MT5 → สแกน "สัญลักษณ์ของโบรก (Exness)" ด้วย CDC บนราคา MT5 จริง
→ ประกอบใบสั่ง (แท่งเทียน/วอลุ่ม/risk/Gemini) → ส่งที่ผ่าน (enter/small) เข้า Telegram
สัญญาณ Part 1 ใช้เป็น 'confluence เสริม' เฉพาะ symbol ที่โบรกมี — ไม่แตะนอกโบรก

รันครั้งเดียว:   python run.py
รันวนต่อเนื่อง:  python run.py --loop
"""
from __future__ import annotations
import logging
import sys
import time

from _config import load
import mt5_client as m
import read_signals
import scan
import ticket as tk
import notify

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("part2.run")

TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# watchlist เริ่มต้น — ใช้ชื่อ "core" (ไม่มี suffix) → resolve เป็นชื่อจริงของโบรกตอนรัน
_DEFAULT_WATCH = [
    # ดัชนี
    "US30", "US500", "USTEC", "DE30", "DE40", "UK100", "JP225", "HK50", "AUS200", "STOXX50",
    # โลหะ + พลังงาน
    "XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "XCUUSD", "USOIL", "UKOIL", "XNGUSD",
    # FX majors + crosses
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    "EURJPY", "GBPJPY", "AUDJPY", "EURGBP", "EURAUD", "GBPAUD", "CADJPY", "NZDJPY",
    # คริปโต
    "BTCUSD", "ETHUSD", "XRPUSD", "LTCUSD", "BCHUSD", "SOLUSD", "ADAUSD", "DOGEUSD", "BNBUSD",
    # หุ้นรายตัว US (subscribe แล้ว · เทรดเฉพาะตลาด US เปิด ~กลางคืนไทย)
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "NFLX", "AMD", "INTC",
    "AVGO", "ORCL", "ADBE", "CSCO", "PYPL", "JPM", "V", "MA", "BAC", "WMT",
    "KO", "PEP", "NKE", "MCD", "BA", "BABA", "F", "XOM", "PFE", "SBUX", "WFC",
]


def _watchlist(cfg: dict, broker: set) -> list[str]:
    from symbol_map import resolve
    raw = cfg.get("SCAN_SYMBOLS", "").strip()
    want = [s.strip() for s in raw.split(",") if s.strip()] if raw else _DEFAULT_WATCH
    have, missing = [], []
    for s in want:
        r = resolve(s, broker)        # XAUUSD → XAUUSD หรือ XAUUSDm ตามชนิดบัญชี
        (have if r else missing).append(r or s)
    if missing:
        log.info("ข้าม (โบรกไม่มี): %s", ", ".join(missing))
    return have


def _part1_hints(cfg: dict, broker: set) -> dict:
    """ดึงสัญญาณ Part 1 → map เป็น symbol Exness (เฉพาะที่โบรกมี) ใช้เป็น confluence"""
    try:
        from symbol_map import map_symbol
        sigs = read_signals.fetch_signals(cfg.get("PART1_BOT_URL", ""), cfg.get("SIGNALS_TOKEN", ""))
        out = {}
        for s in sigs:
            ex = map_symbol(s, broker)
            if ex:
                out[ex] = s
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("ดึง Part 1 hints ไม่สำเร็จ: %s", e)
        return {}


def run_once(cfg: dict) -> int:
    if not m.connect(path=TERMINAL, login=int(cfg["MT5_LOGIN"]),
                     password=cfg["MT5_PASSWORD"], server=cfg["MT5_SERVER"]):
        log.error("ต่อ MT5 ไม่ได้ — ข้ามรอบนี้")
        return 0
    try:
        acc = m.account()
        broker = set(m.list_symbols())
        watch = _watchlist(cfg, broker)
        hints = _part1_hints(cfg, broker)
        accept = {x.strip() for x in cfg.get("GEMINI_ACCEPT", "enter,small").split(",") if x.strip()}

        # รวม universe: watchlist + หุ้นที่ Part 1 ส่งสัญญาณและโบรกมี (ดึงเข้ามาพิจารณาอัตโนมัติ)
        from_p1 = [s for s in hints if s not in watch]
        universe = watch + from_p1
        log.info("สแกนโบรก %d สัญลักษณ์ (watchlist %d + จาก Part 1 %d: %s)",
                 len(universe), len(watch), len(from_p1), ", ".join(from_p1) or "-")

        biases = scan.scan_broker(universe, m, cfg)
        log.info("เจอ %d ตัวมีทิศ (ไม่ sideway)", len(biases))

        sent = 0
        for b in biases:
            ex = b["symbol"]
            try:
                t = tk.build_ticket(ex, b, acc, cfg, m, part1_hint=hints.get(ex))
            except Exception as e:  # noqa: BLE001
                log.warning("build_ticket %s ล้มเหลว: %s", ex, e)
                continue
            if not t or t.get("skipped"):
                continue
            dec = t["verdict"].get("decision")
            if dec not in accept:
                log.info("⛔ ข้าม %s (Gemini: %s) %s", ex, dec, t["verdict"].get("reason"))
                continue
            ok = notify.send(tk.format_ticket(t), cfg.get("TELEGRAM_BOT_TOKEN", ""),
                             cfg.get("TELEGRAM_CHAT_ID", ""))
            log.info("ส่งใบสั่ง %s (%s) → %s", ex, dec, ok)
            sent += 1
        log.info("รอบนี้ส่ง %d ใบสั่ง", sent)
        return sent
    finally:
        m.shutdown()


def main():
    cfg = load()
    if "--loop" in sys.argv:
        every = int(float(cfg.get("POLL_MINUTES", "15")) * 60)
        log.info("โหมด loop ทุก %d วินาที (Ctrl+C หยุด)", every)
        while True:
            try:
                run_once(cfg)
            except Exception as e:  # noqa: BLE001
                log.exception("รอบล้มเหลว: %s", e)
            time.sleep(every)
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
