"""
core/indicators.py — เขียน indicator เองด้วย pandas/numpy
เหตุผล: ไม่ดึง pandas_ta / TA-Lib เข้ามา (image จะบวมเกิน 500MB)
สูตรอ้างอิงตามมาตรฐาน (Wilder smoothing สำหรับ RSI/ADX)
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average — ใช้ adjust=False ให้ตรงกับ TradingView/Pine"""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI สูตร Wilder (EMA-like smoothing ที่ alpha = 1/period)"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing = EMA แบบ alpha=1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # ถ้า avg_loss = 0 และ avg_gain > 0 → RSI = 100
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    # ถ้าทั้งคู่ = 0 → ราคานิ่ง → RSI = 50 (ถือว่ากลาง)
    out = out.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
    return out


def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """
    ADX สูตร Wilder ดั้งเดิม
    คืนค่าเป็น series ของ ADX อย่างเดียว (ไม่คืน +DI / -DI เพราะเราไม่ใช้)
    """
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=period).mean() / atr

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    out = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    return out


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range (Wilder) — ใช้คำนวณ SL/TP ตามความผันผวนจริง"""
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
