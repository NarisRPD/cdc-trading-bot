"""
data/stocks.py — ดึงข้อมูลหุ้น (US + Thai) ผ่าน yfinance + Stooq fallback

หลัก:
1. ใช้ yf.download() แบบ batch (group_by='ticker') ลด rate limit
2. ตัวที่ยังเป็นแท่งวันนี้ที่ตลาดยังไม่ปิด → ตัดทิ้ง
3. ตัวที่ NaN เยอะ / แถวไม่พอ → คืน None (caller skip)
4. ถ้าทั้ง batch ของ yfinance ว่าง → ลอง Stooq ทีละตัว
"""
from __future__ import annotations
import logging
from typing import Dict, Iterable, List, Optional

import pandas as pd

from ._retry import retry

log = logging.getLogger(__name__)


def _to_lower_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """แปลงคอลัมน์ Open/High/Low/Close/Volume → ตัวพิมพ์เล็ก"""
    rename = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ("open", "high", "low", "close", "volume", "adj close"):
            rename[c] = "adj_close" if cl == "adj close" else cl
    df = df.rename(columns=rename)
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    return df[keep]


def _trim_unclosed_bar(df: pd.DataFrame, tz: Optional[str] = None) -> pd.DataFrame:
    """
    yfinance daily: index คือ "วันเริ่มแท่ง"
    ตัดแท่งสุดท้ายทิ้งถ้ายังเป็นวันนี้ (ตลาดยังไม่ปิด/เพิ่งปิดยังไม่อัปเดต)
    ใช้ทั้งหุ้น US, Thai, commodities ได้
    """
    if df is None or df.empty:
        return df
    # ใช้ UTC normalize เปรียบเทียบกับ index (yfinance index เป็น tz-naive วันเริ่มแท่ง)
    today_utc = pd.Timestamp.utcnow().normalize().tz_localize(None)
    last_idx = pd.Timestamp(df.index[-1]).normalize()
    # ปลอดภัยเสมอ: ถ้า index ล่าสุด ≥ วันนี้ (UTC) → ถือเป็นแท่งยังไม่ปิด → ตัด
    # (กรณีตลาดเอเชียปิดก่อน UTC midnight เราเสีย 1 แท่งของเมื่อวาน แต่ no-repaint สำคัญกว่า)
    if last_idx >= today_utc:
        df = df.iloc[:-1]
    return df


def _yf_batch_download(
    tickers: List[str],
    period: str = "2y",
) -> Dict[str, pd.DataFrame]:
    """
    download batch ผ่าน yfinance, คืน dict ticker → DataFrame ที่ normalize + ตัดแท่งยังไม่ปิดแล้ว
    ตัวไหน NaN/ว่าง จะไม่อยู่ใน dict ที่คืน
    """
    import yfinance as yf

    def _download() -> pd.DataFrame:
        return yf.download(
            tickers=" ".join(tickers),
            period=period,
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )

    try:
        raw = retry(_download, attempts=3, base_delay=2.0, label="yf.download")
    except Exception as e:  # noqa: BLE001
        log.error("yfinance batch download failed: %s", e)
        return {}

    out: Dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out

    if isinstance(raw.columns, pd.MultiIndex):
        # หลายตัว → top-level = ticker
        for t in tickers:
            if t not in raw.columns.get_level_values(0):
                continue
            sub = raw[t].copy()
            sub = _to_lower_ohlcv(sub).dropna(how="all")
            sub = _trim_unclosed_bar(sub)
            if not sub.empty and "close" in sub.columns:
                out[t] = sub
    else:
        # ตัวเดียว → flat columns
        if len(tickers) == 1:
            sub = _to_lower_ohlcv(raw).dropna(how="all")
            sub = _trim_unclosed_bar(sub)
            if not sub.empty:
                out[tickers[0]] = sub
    return out


def _stooq_fetch_single(ticker: str) -> Optional[pd.DataFrame]:
    """
    fallback ผ่าน Stooq (EOD ฟรี เสถียร)
    yahoo ticker → Stooq mapping:
      - US stocks: 'AAPL' → 'aapl.us'
      - Commodities ('GC=F') มี mapping เฉพาะใน commodities.py
    """
    try:
        from pandas_datareader import data as pdr
    except Exception as e:  # noqa: BLE001
        log.warning("pandas_datareader ไม่พร้อม: %s", e)
        return None

    symbol = ticker.lower()
    if "." not in symbol and "=" not in symbol:
        symbol = f"{symbol}.us"

    def _go() -> pd.DataFrame:
        return pdr.DataReader(symbol, "stooq")

    try:
        # attempts=1: ถ้า Stooq พัง มันมักพังแบบ deterministic (endpoint เปลี่ยน)
        # retry ไม่ช่วย แค่ถ่วงเวลา — ให้ circuit breaker ใน fetch_stocks_batch คุมแทน
        df = retry(_go, attempts=1, base_delay=1.0, label=f"stooq({ticker})")
    except Exception as e:  # noqa: BLE001
        log.warning("stooq fetch failed for %s (%s): %s", ticker, symbol, e)
        return None

    if df is None or df.empty:
        return None

    df = df.sort_index()
    df.columns = [c.lower() for c in df.columns]
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep]
    df = _trim_unclosed_bar(df)
    if df.empty:
        return None
    return df


def fetch_stocks_batch(
    tickers: Iterable[str],
    *,
    period: str = "2y",
    chunk_size: int = 100,
) -> Dict[str, pd.DataFrame]:
    """
    Public API: ดึงหุ้นเป็น batch (US หรือ Thai .BK ก็ได้)
    ตัวที่ batch ว่าง → fallback Stooq ทีละตัว
    ตัวที่ NaN/ไม่พอ ก็ปล่อยให้ signals.compute_signal ทำงานต่อแล้ว return None
    """
    tickers = list(dict.fromkeys(tickers))
    results: Dict[str, pd.DataFrame] = {}

    # yfinance: batch ทีละ chunk เลี่ยง URL ยาวเกิน + rate limit
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i : i + chunk_size]
        log.info("yfinance batch %d-%d / %d", i + 1, i + len(chunk), len(tickers))
        results.update(_yf_batch_download(chunk, period=period))

    # หาตัวที่หายไป → fallback Stooq (มี circuit breaker กัน Stooq ล่มแล้วลากยาว)
    missing = [t for t in tickers if t not in results]
    if missing:
        log.info("yfinance ขาด %d ตัว → ลอง Stooq fallback", len(missing))
        recovered = 0
        consecutive_fail = 0
        # ถ้า Stooq พังติดกันเกินเกณฑ์ = endpoint ใช้ไม่ได้ → เลิกลองที่เหลือทันที
        stooq_fail_limit = 5
        for idx, t in enumerate(missing):
            if consecutive_fail >= stooq_fail_limit:
                log.warning(
                    "Stooq พังติดกัน %d ครั้ง → ข้ามที่เหลืออีก %d ตัว (ถือว่า Stooq ใช้ไม่ได้ตอนนี้)",
                    stooq_fail_limit, len(missing) - idx,
                )
                break
            df = _stooq_fetch_single(t)
            if df is not None and len(df) >= 60:
                results[t] = df
                recovered += 1
                consecutive_fail = 0
            else:
                consecutive_fail += 1
        log.info("Stooq recovered: %d / %d", recovered, len(missing))

    log.info("fetch_stocks_batch: %d ตัวสำเร็จ จากที่ขอ %d ตัว",
             len(results), len(tickers))
    return results
