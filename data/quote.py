"""
data/quote.py — ดึงข้อมูล "รายตัว" สำหรับ watchlist
ใช้ adapter เดิม (crypto/stocks) ไม่ทำซ้ำ logic การ fetch/ตัดแท่งยังไม่ปิด

- fetch_history(market, ticker): คืน DataFrame OHLCV daily (ตัดแท่งยังไม่ปิดแล้ว) หรือ None
- last_price(market, ticker): คืนราคาล่าสุด (เร็ว) หรือ None
"""
from __future__ import annotations
import logging
from functools import lru_cache
from typing import Optional

import pandas as pd

from core.symbols import Market
from data.crypto import fetch_ohlcv_daily
from data.stocks import _yf_batch_download

log = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _crypto_exchange(name: str):
    """สร้าง ccxt exchange ครั้งเดียวต่อชื่อ (cache)"""
    import ccxt

    factory = {"binance": ccxt.binance, "bybit": ccxt.bybit, "okx": ccxt.okx}
    ex = factory.get(name, ccxt.binance)({"enableRateLimit": True})
    return ex


def fetch_history(market: Market, ticker: str, *, crypto_exchange: str = "binance") -> Optional[pd.DataFrame]:
    """ดึง OHLCV daily ของ symbol เดียว — คืน None ถ้าพัง"""
    try:
        if market == "crypto":
            ex = _crypto_exchange(crypto_exchange)
            return fetch_ohlcv_daily(ex, ticker)
        # us / thai / commodity → yfinance
        out = _yf_batch_download([ticker], period="2y")
        return out.get(ticker)
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_history failed for %s (%s): %s", ticker, market, e)
        return None


def last_price(market: Market, ticker: str, *, crypto_exchange: str = "binance") -> Optional[float]:
    """ราคาล่าสุด (เร็ว) — crypto ใช้ ticker, หุ้น/โลหะใช้ fast_info; fallback เป็น close ล่าสุด"""
    try:
        if market == "crypto":
            ex = _crypto_exchange(crypto_exchange)
            t = ex.fetch_ticker(ticker)
            return float(t["last"]) if t.get("last") is not None else None
        import yfinance as yf

        fi = yf.Ticker(ticker).fast_info
        px = fi.get("last_price") if hasattr(fi, "get") else getattr(fi, "last_price", None)
        if px:
            return float(px)
    except Exception as e:  # noqa: BLE001
        log.warning("last_price fast path failed for %s: %s — fallback history", ticker, e)

    # fallback: close ล่าสุดจาก history
    df = fetch_history(market, ticker, crypto_exchange=crypto_exchange)
    if df is not None and not df.empty:
        return float(df["close"].iloc[-1])
    return None
