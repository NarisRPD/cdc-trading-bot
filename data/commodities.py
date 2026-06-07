"""
data/commodities.py — ทองคำ / แร่เงิน / ทองแดง ผ่าน yfinance
fallback Stooq สำหรับทอง (XAUUSD)
"""
from __future__ import annotations
import logging
from typing import Dict, Optional

import pandas as pd

from .stocks import _yf_batch_download, _stooq_fetch_single  # reuse helpers

log = logging.getLogger(__name__)

# Yahoo ticker → display name
COMMODITIES: Dict[str, str] = {
    "GC=F": "Gold (GC=F)",
    "SI=F": "Silver (SI=F)",
    "HG=F": "Copper (HG=F)",
}

# fallback Stooq mapping (ใช้ตอน yfinance ไม่คืนค่า)
_STOOQ_FALLBACK = {
    "GC=F": "xauusd",
    "SI=F": "xagusd",
    "HG=F": "hg.f",
}


def fetch_commodities() -> Dict[str, pd.DataFrame]:
    """
    คืน dict yahoo_ticker → DataFrame (ohlcv)
    ตัวไหนพังก็ข้าม
    """
    tickers = list(COMMODITIES.keys())
    results = _yf_batch_download(tickers, period="2y")
    missing = [t for t in tickers if t not in results]

    for t in missing:
        stooq_sym = _STOOQ_FALLBACK.get(t)
        if not stooq_sym:
            continue
        log.info("commodity %s → ลอง Stooq (%s)", t, stooq_sym)
        df = _stooq_fetch_single(stooq_sym)
        if df is not None and len(df) >= 60:
            results[t] = df

    log.info("commodities loaded: %d / %d", len(results), len(tickers))
    return results
