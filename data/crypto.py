"""
data/crypto.py — ดึงข้อมูล crypto ผ่าน ccxt
default: Binance (Cloud Run สิงคโปร์ใช้ได้)
fallback: Bybit / OKX (เผื่อ Binance ตอบ 451 — แม้ Cloud Run สิงคโปร์ก็เคยโดน)

หน้าที่:
1. ดึง 24h ticker → คัด Top N USDT pairs (filter stablecoin + leveraged)
2. ดึง OHLCV daily ของแต่ละคู่ → คืน DataFrame ที่ "ตัดแท่งยังไม่ปิดออกแล้ว"
"""
from __future__ import annotations
import logging
import re
from typing import Dict, List, Optional

import ccxt
import pandas as pd

from ._retry import retry

log = logging.getLogger(__name__)

# stablecoins ที่ต้องคัดทิ้ง (base side)
_STABLE_BASES = {
    "USDC", "USDT", "BUSD", "FDUSD", "DAI", "TUSD", "USDE", "USDP",
    "USDS", "PYUSD", "GUSD", "EURT", "EURS", "EURI", "FRAX", "LUSD",
    "USDD", "USDJ", "USTC",
}

# leveraged token pattern
_LEVERAGED_RE = re.compile(r"(UP|DOWN|BULL|BEAR|3L|3S|5L|5S)$", re.IGNORECASE)


def _make_exchange(name: str) -> ccxt.Exchange:
    name = name.lower().strip()
    if name == "binance":
        return ccxt.binance({"enableRateLimit": True})
    if name == "bybit":
        return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    if name == "okx":
        return ccxt.okx({"enableRateLimit": True})
    raise ValueError(f"unsupported exchange: {name}")


def _is_clean_spot_pair(symbol: str, market: dict) -> bool:
    """spot only, /USDT only, ไม่ใช่ stablecoin/leveraged"""
    if not market.get("spot", False):
        return False
    if not market.get("active", True):
        return False
    if market.get("quote") != "USDT":
        return False
    base = market.get("base", "").upper()
    if base in _STABLE_BASES:
        return False
    if _LEVERAGED_RE.search(base):
        return False
    return True


def select_top_symbols(
    exchange_name: str,
    top_n: int = 50,
    min_volume_usdt: float = 1_000_000.0,
) -> tuple[ccxt.Exchange, List[str]]:
    """
    คืน (exchange instance, list ของ symbol ที่จะสแกน) — เรียงตาม 24h quote volume
    raise ถ้า exchange ตอบไม่ได้เลย (caller decide ว่าจะ fallback ไป exchange อื่นไหม)
    """
    ex = _make_exchange(exchange_name)
    markets = retry(ex.load_markets, attempts=3, base_delay=1.5,
                    label=f"{exchange_name}.load_markets")
    tickers = retry(lambda: ex.fetch_tickers(), attempts=3, base_delay=1.5,
                    label=f"{exchange_name}.fetch_tickers")

    candidates: list[tuple[str, float]] = []
    for sym, t in tickers.items():
        m = markets.get(sym)
        if not m or not _is_clean_spot_pair(sym, m):
            continue
        # quote volume = volume × avg price ≈ 24h notional ในหน่วย USDT
        quote_vol = t.get("quoteVolume")
        if quote_vol is None:
            base_vol = t.get("baseVolume") or 0
            last = t.get("last") or 0
            quote_vol = base_vol * last
        if quote_vol < min_volume_usdt:
            continue
        candidates.append((sym, float(quote_vol)))

    candidates.sort(key=lambda x: x[1], reverse=True)
    chosen = [s for s, _ in candidates[:top_n]]
    log.info("[%s] universe: %d candidates → top %d",
             exchange_name, len(candidates), len(chosen))
    return ex, chosen


def fetch_ohlcv_daily(
    ex: ccxt.Exchange,
    symbol: str,
    *,
    limit: int = 320,
) -> Optional[pd.DataFrame]:
    """
    ดึง OHLCV daily แล้ว "ตัดแท่งที่ยังไม่ปิด" ทิ้ง
    คืน None ถ้าเฟล (จะถูกข้ามใน main loop)
    """
    try:
        raw = retry(
            lambda: ex.fetch_ohlcv(symbol, timeframe="1d", limit=limit),
            attempts=3, base_delay=2.0, label=f"fetch_ohlcv({symbol})",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("crypto fetch failed for %s: %s", symbol, e)
        return None

    if not raw or len(raw) < 30:
        return None

    df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").sort_index()

    # crypto trade 24/7 → แท่งสุดท้ายจาก ccxt เกือบเสมอเป็นแท่ง "วันนี้ที่ยังก่อตัว"
    # ตัดออกถ้า ts ของแท่งสุดท้าย ≥ เริ่มต้นวันนี้ UTC
    now_utc = pd.Timestamp.now(tz="UTC").normalize()
    if df.index[-1] >= now_utc:
        df = df.iloc[:-1]

    if len(df) < 30:
        return None
    return df


def fetch_crypto_universe(
    primary_exchange: str = "binance",
    *,
    top_n: int = 50,
    min_volume_usdt: float = 1_000_000.0,
) -> tuple[Optional[ccxt.Exchange], List[str]]:
    """
    ลอง primary ก่อน ถ้าพังให้ลอง bybit → okx
    คืน None ถ้าใช้ไม่ได้สักตัว
    """
    order = [primary_exchange]
    for alt in ("bybit", "okx"):
        if alt not in order:
            order.append(alt)

    for name in order:
        try:
            ex, syms = select_top_symbols(name, top_n=top_n,
                                          min_volume_usdt=min_volume_usdt)
            if syms:
                log.info("crypto exchange selected: %s (%d symbols)", name, len(syms))
                return ex, syms
        except Exception as e:  # noqa: BLE001
            # ตรวจ 451 explicitly เพื่อ log ชัด ๆ ตามสเปก
            msg = str(e)
            if "451" in msg or "restricted location" in msg.lower():
                log.error("[%s] HTTP 451 — IP ถูกบล็อก, ลอง exchange ถัดไป", name)
            else:
                log.warning("[%s] universe load failed: %s", name, e)
            continue

    log.error("ไม่มี crypto exchange ตัวไหนใช้ได้ — ข้ามกลุ่ม crypto")
    return None, []
