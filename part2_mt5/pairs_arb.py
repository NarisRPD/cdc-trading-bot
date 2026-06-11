"""
part2_mt5/pairs_arb.py — Pairs Trading / Statistical Arbitrage (สไตล์ Citadel/quant fund)

หลักการ: เทรด "ส่วนต่าง (spread)" ของคู่สินทรัพย์ที่วิ่งสัมพันธ์กัน — market-neutral
ไม่เดิมพันทิศตลาด เดิมพันว่า spread ที่ถ่างผิดปกติจะหุบกลับสู่ค่าเฉลี่ย

  spread = log(A) - beta×log(B)   (beta = rolling OLS hedge ratio)
  z      = (spread - mean) / std  บนหน้าต่าง PAIRS_LOOKBACK แท่ง

  z ≥ +Z_ENTRY → A แพงผิดปกติ → SELL A + BUY B   (รอ z หุบกลับ)
  z ≤ -Z_ENTRY → A ถูกผิดปกติ → BUY A + SELL B
  |z| ≤ Z_EXIT → spread กลับสู่ค่าเฉลี่ย → ปิดทั้งสองขา (take profit)
  |z| ≥ Z_STOP → ความสัมพันธ์พัง (regime break) → ตัดขาดทุนทั้งคู่
  อายุเกิน PAIRS_MAX_HOURS → ปิด (time stop — spread ไม่หุบตามคาด)

แยกขาดจากระบบหลัก:
  - magic 260606 (ไม่ใช่ 260605) → manage_positions/journal/learn ไม่แตะขา pair
    (trailing/partial รายขาจะทำลายความ market-neutral — ห้ามใช้)
  - มี catastrophic SL ต่อขา (PAIRS_CAT_SL_PCT) กันบอท/VPS ตายแล้วขาลอย
  - state เก็บใน part2_pairs.json — restart แล้วจัดการไม้เดิมต่อได้
  - แจ้ง Telegram เปิด/ปิดเองพร้อม P/L รวมของคู่

ใช้: เรียก pairs_arb.tick(cfg, mt5c, token, chat) ใน main loop (throttle ตัวเองทุก PAIRS_CHECK_SEC)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger("part2.pairs")

_MAGIC = 260606                      # แยกจาก Part 2 หลัก (260605) — ระบบอื่นไม่แตะขา pair
_FILE = os.path.join(os.path.dirname(__file__), "part2_pairs.json")
_last_check = 0.0                    # throttle
_fail_until: dict = {}               # {pair_id: ts} — cooldown หลังเปิดขาไม่สำเร็จ
                                     # (กัน churn: z ค้างสูง → ลองใหม่ทุก tick → เปิด/ปิดขา A ซ้ำๆ เสีย spread ฟรี)


# ── state ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        if os.path.exists(_FILE):
            with open(_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save(d: dict) -> None:
    try:
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _FILE)
    except Exception as e:  # noqa: BLE001
        log.warning("save pairs state fail: %s", e)


# ── คณิตศาสตร์ spread ──────────────────────────────────────────────────────

def zscore(close_a: np.ndarray, close_b: np.ndarray, lookback: int = 96) -> "dict | None":
    """คำนวณ z-score ของ spread (log A - beta×log B) บนหน้าต่าง lookback แท่งล่าสุด
    คืน {z, beta, corr, mean, std} หรือ None ถ้าข้อมูลไม่พอ/std เพี้ยน
    *** ใช้เฉพาะแท่งปิดแล้ว — ผู้เรียกต้องตัดแท่ง forming ออกก่อน ***"""
    n = min(len(close_a), len(close_b))
    if n < lookback + 5:
        return None
    la = np.log(close_a[-lookback:].astype(float))
    lb = np.log(close_b[-lookback:].astype(float))
    var_b = float(np.var(lb))
    if var_b <= 1e-12:
        return None
    beta = float(np.cov(la, lb)[0, 1] / var_b)
    spread = la - beta * lb
    mu, sd = float(spread.mean()), float(spread.std())
    if sd <= 1e-12:
        return None
    corr = float(np.corrcoef(la, lb)[0, 1])
    return {"z": float((spread[-1] - mu) / sd), "beta": round(beta, 4),
            "corr": round(corr, 3), "mean": mu, "std": sd}


# ── broker helpers ─────────────────────────────────────────────────────────

def _resolve(sym: str, broker: set) -> "str | None":
    """หา symbol จริงของโบรก: ลองชื่อตรง → ชื่อ+'m' (Exness suffix) → None"""
    if sym in broker:
        return sym
    if sym + "m" in broker:
        return sym + "m"
    return None


def _lots_for_notional(exsym: str, notional: float) -> float:
    """แปลง notional (USD) → lots ตามสเปกโบรก (ปัดลงตาม step · อย่างน้อย volume_min)"""
    import MetaTrader5 as m5
    info = m5.symbol_info(exsym)
    tick = m5.symbol_info_tick(exsym)
    if not info or not tick or not tick.ask:
        return 0.0
    contract = info.trade_contract_size or 1.0
    raw = notional / (tick.ask * contract)
    step = info.volume_step or 0.01
    lots = max(round(raw / step) * step, info.volume_min or 0.01)
    return round(min(lots, info.volume_max or lots), 2)


def _open_leg(exsym: str, direction: str, lots: float, cat_sl_pct: float) -> "dict | None":
    """เปิดขา 1 ข้างของ pair — market order + catastrophic SL (กันบอทตายแล้วขาลอย)
    คืน {ticket, price} หรือ None ถ้าพลาด"""
    import MetaTrader5 as m5
    m5.symbol_select(exsym, True)
    tick = m5.symbol_info_tick(exsym)
    if tick is None:
        return None
    is_buy = direction == "buy"
    price = tick.ask if is_buy else tick.bid
    # catastrophic SL: ไกลพอไม่โดน noise — แค่กันหายนะ ไม่ใช่ SL กลยุทธ์ (exit จริงคือ z)
    sl = price * (1 - cat_sl_pct / 100) if is_buy else price * (1 + cat_sl_pct / 100)
    base = {"action": m5.TRADE_ACTION_DEAL, "symbol": exsym, "volume": float(lots),
            "type": m5.ORDER_TYPE_BUY if is_buy else m5.ORDER_TYPE_SELL,
            "price": price, "sl": round(sl, 5), "deviation": 30,
            "magic": _MAGIC, "comment": "Part2-Pairs", "type_time": m5.ORDER_TIME_GTC}
    for fill in (m5.ORDER_FILLING_IOC, m5.ORDER_FILLING_FOK, m5.ORDER_FILLING_RETURN):
        res = m5.order_send({**base, "type_filling": fill})
        if res is None:
            return None
        if res.retcode == m5.TRADE_RETCODE_DONE:
            # หา position ticket จริง (ECN: order ≠ position)
            time.sleep(0.2)
            pos_t = res.order
            for p in (m5.positions_get(symbol=exsym) or []):
                if p.magic == _MAGIC and abs(p.volume - lots) < 1e-9:
                    pos_t = p.ticket
            return {"ticket": int(pos_t), "price": float(res.price or price)}
        if res.retcode != 10030:
            log.warning("pairs เปิดขา %s %s fail: retcode=%s %s", exsym, direction,
                        res.retcode, res.comment)
            return None
    return None


def _close_leg(ticket: int) -> float:
    """ปิดขาตาม position ticket → คืน profit ของขานั้น (0 ถ้าไม่พบ = ปิดไปแล้ว เช่นโดน cat SL)"""
    import MetaTrader5 as m5
    import execute
    pos = m5.positions_get(ticket=int(ticket))
    if not pos:
        return 0.0
    p = pos[0]
    profit = float(p.profit)
    execute.close_position(p)          # ใช้ retry filling logic เดิม (magic ใน comment ไม่สำคัญตอนปิด)
    return profit


# ── core ───────────────────────────────────────────────────────────────────

def _cfgf(cfg, key, default):
    try:
        return float(cfg.get(key, str(default)) or str(default))
    except (ValueError, TypeError):
        return float(default)


def tick(cfg: dict, mt5c, token: str = "", chat: str = "") -> None:
    """เรียกทุก loop จาก interactive — ตรวจ entry/exit ของทุกคู่ (throttle ในตัว)
    mt5c = โมดูล mt5_client (ใช้ .rates) · token/chat = Telegram (ว่าง = ไม่แจ้ง)"""
    global _last_check
    if cfg.get("USE_PAIRS_ARB", "false").lower() not in ("1", "true", "yes", "on"):
        return
    now = time.time()
    if now - _last_check < _cfgf(cfg, "PAIRS_CHECK_SEC", 60):
        return
    _last_check = now

    try:
        _tick_inner(cfg, mt5c, token, chat)
    except Exception as e:  # noqa: BLE001
        log.warning("pairs tick error (ข้ามรอบนี้): %s", e)


def _notify(token, chat, msg):
    if token and chat:
        try:
            import tg
            tg.send_text(token, chat, msg)
        except Exception:  # noqa: BLE001
            pass


def _tick_inner(cfg: dict, mt5c, token: str, chat: str) -> None:
    import MetaTrader5 as m5

    tf        = cfg.get("PAIRS_TF", "M15")
    lookback  = int(_cfgf(cfg, "PAIRS_LOOKBACK", 96))
    z_entry   = _cfgf(cfg, "PAIRS_Z_ENTRY", 2.0)
    z_exit    = _cfgf(cfg, "PAIRS_Z_EXIT", 0.5)
    z_stop    = _cfgf(cfg, "PAIRS_Z_STOP", 3.5)
    min_corr  = _cfgf(cfg, "PAIRS_MIN_CORR", 0.70)
    max_open  = int(_cfgf(cfg, "PAIRS_MAX_OPEN", 1))
    max_hours = _cfgf(cfg, "PAIRS_MAX_HOURS", 24)
    cat_sl    = _cfgf(cfg, "PAIRS_CAT_SL_PCT", 8.0)
    notion_x  = _cfgf(cfg, "PAIRS_NOTIONAL_X", 1.0)   # notional ต่อขา = balance × ค่านี้

    broker = set(mt5c.list_symbols())
    acc = mt5c.account() or {}
    bal = acc.get("balance", 0) or 0
    if bal <= 0:
        return
    notional = bal * notion_x

    state = _load()
    pairs_raw = cfg.get("PAIRS_LIST", "BTCUSD:ETHUSD,US500:US30")
    changed = False

    for pair_str in (p.strip() for p in pairs_raw.split(",") if p.strip()):
        if ":" not in pair_str:
            continue
        a_name, b_name = (s.strip() for s in pair_str.split(":", 1))
        sym_a = _resolve(a_name, broker)
        sym_b = _resolve(b_name, broker)
        if not sym_a or not sym_b:
            continue

        import market_hours
        if not (market_hours.is_open(sym_a) and market_hours.is_open(sym_b)):
            continue                                   # ขาใดขาหนึ่งตลาดปิด → ข้าม (เปิด/ปิดต้องทำได้ทั้งคู่)

        df_a = mt5c.rates(sym_a, tf, lookback + 10)
        df_b = mt5c.rates(sym_b, tf, lookback + 10)
        if df_a is None or df_b is None or len(df_a) < lookback + 2 or len(df_b) < lookback + 2:
            continue
        # ตัดแท่ง forming — ตัดสินใจจากแท่งปิดแล้วเท่านั้น
        ca = df_a["close"].to_numpy(float)[:-1]
        cb = df_b["close"].to_numpy(float)[:-1]
        zs = zscore(ca, cb, lookback)
        if zs is None:
            continue
        z = zs["z"]
        pid = f"{sym_a}:{sym_b}"
        pos = state.get(pid)

        # ── จัดการคู่ที่เปิดอยู่ ──────────────────────────────────────────
        if pos:
            age_h = (datetime.now(timezone.utc).timestamp() - pos["opened_at"]) / 3600
            # ขาใดหายไป (โดน catastrophic SL) → ปิดอีกขาทันที กัน exposure ข้างเดียว
            la = m5.positions_get(ticket=int(pos["leg_a"]["ticket"]))
            lb = m5.positions_get(ticket=int(pos["leg_b"]["ticket"]))
            leg_lost = (not la) or (not lb)

            reason = None
            if leg_lost:                 reason = "ขาหนึ่งโดน SL — ปิดขาที่เหลือกัน exposure"
            elif abs(z) <= z_exit:       reason = f"z หุบกลับ {z:+.2f} ≤ {z_exit} — take profit"
            elif abs(z) >= z_stop:       reason = f"z ถ่างต่อ {z:+.2f} ≥ {z_stop} — regime break ตัดขาดทุน"
            elif age_h >= max_hours:     reason = f"ถือครบ {age_h:.0f} ชม. — time stop"

            if reason:
                pnl = _close_leg(pos["leg_a"]["ticket"]) + _close_leg(pos["leg_b"]["ticket"])
                emo = "✅" if pnl >= 0 else "❌"
                log.info("pairs ปิด %s — %s · P/L $%.2f", pid, reason, pnl)
                _notify(token, chat,
                        f"{emo} ปิดคู่ Pairs — {pid}\n⚖️ {reason}\n💰 P/L รวม ${pnl:+.2f}")
                state.pop(pid, None)
                changed = True
            continue

        # ── หา entry ใหม่ ────────────────────────────────────────────────
        if len(state) >= max_open:
            continue
        if time.time() < _fail_until.get(pid, 0):
            continue                               # เพิ่งเปิดขาไม่สำเร็จ — พัก cooldown ก่อน
        if abs(z) < z_entry or abs(z) >= z_stop:
            continue
        # ความสัมพันธ์ต้อง "แรง" — ทิศไหนก็ได้: +0.7 ขึ้น (วิ่งตามกัน) หรือ -0.7 ลง (วิ่งสวนกันเสถียร)
        # ใช้ไม่ได้คือ |corr| ต่ำ = ต่างคนต่างวิ่งจริงๆ ไม่มีเชือกผูก ไม่มีอะไรให้หุบกลับ
        if abs(zs["corr"]) < min_corr:
            log.info("pairs ข้าม %s — |corr| %.2f < %.2f (ความสัมพันธ์ไม่แรงพอ)", pid, zs["corr"], min_corr)
            continue

        # ทิศขา A: z > 0 = A แพงเกินความสัมพันธ์ → sell A · z < 0 = กลับด้าน
        # ทิศขา B ขึ้นกับ "เครื่องหมาย beta":
        #   beta > 0 (คู่วิ่งตามกัน)  → ขาตรงข้าม A (hedge แบบคลาสสิก)
        #   beta < 0 (คู่วิ่งสวนกัน)  → ขา "เดียวกับ" A — เพราะความสัมพันธ์เป็นกระจกเงา
        #   การถือตรงข้ามบนคู่สวนกัน = เดิมพันซ้ำสองเท่า ไม่ใช่ hedge!
        dir_a = "sell" if z > 0 else "buy"
        if zs["beta"] >= 0:
            dir_b = "buy" if z > 0 else "sell"
        else:
            dir_b = dir_a
        lots_a = _lots_for_notional(sym_a, notional)
        lots_b = _lots_for_notional(sym_b, notional)
        if lots_a <= 0 or lots_b <= 0:
            continue

        _cool = float(_cfgf(cfg, "PAIRS_FAIL_COOLDOWN_MIN", 30)) * 60
        leg_a = _open_leg(sym_a, dir_a, lots_a, cat_sl)
        if not leg_a:
            _fail_until[pid] = time.time() + _cool
            log.warning("pairs %s: ขา A ไม่ติด → พัก %d นาที", pid, int(_cool / 60))
            continue
        leg_b = _open_leg(sym_b, dir_b, lots_b, cat_sl)
        if not leg_b:
            # ขา B ไม่ติด → ปิดขา A ทันที (ห้ามถือขาเดียว — ไม่ใช่ stat-arb แล้ว)
            _close_leg(leg_a["ticket"])
            _fail_until[pid] = time.time() + _cool
            log.warning("pairs %s: ขา B ไม่ติด → ยกเลิกขา A แล้ว · พัก %d นาที", pid, int(_cool / 60))
            continue

        state[pid] = {
            "leg_a": {"sym": sym_a, "dir": dir_a, "lots": lots_a, **leg_a},
            "leg_b": {"sym": sym_b, "dir": dir_b, "lots": lots_b, **leg_b},
            "z_entry": round(z, 2), "beta": zs["beta"],
            "opened_at": datetime.now(timezone.utc).timestamp(),
        }
        changed = True
        log.info("pairs เปิด %s — z=%+.2f beta=%.2f corr=%.2f · %s %s %.2f / %s %s %.2f",
                 pid, z, zs["beta"], zs["corr"], dir_a, sym_a, lots_a, dir_b, sym_b, lots_b)
        _rel = "↔️ คู่วิ่งสวนกัน (inverse)" if zs["beta"] < 0 else "↗️ คู่วิ่งตามกัน"
        _notify(token, chat,
                f"⚖️ เปิดคู่ Pairs (Stat-Arb) — {pid}\n"
                f"📐 z-score {z:+.2f} (เข้าที่ ±{z_entry}) · corr {zs['corr']:+.2f} · {_rel}\n"
                f"🔴 {dir_a.upper()} {sym_a} {lots_a} lot · 🟢 {dir_b.upper()} {sym_b} {lots_b} lot\n"
                f"🎯 ออกเมื่อ z หุบ ≤ ±{z_exit} · ตัดที่ ±{z_stop} หรือ {max_hours:.0f} ชม.\n"
                f"ℹ️ market-neutral — ไม่เดิมพันทิศตลาด")

    if changed:
        _save(state)
