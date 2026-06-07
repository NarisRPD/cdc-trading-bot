"""
universe/nasdaq100.py — รายชื่อ NASDAQ-100 (Wikipedia + static fallback)
"""
from __future__ import annotations
from typing import List

from universe._wiki import fetch_wiki_tickers

_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

# static fallback (snapshot ~2025) — ใช้เมื่อ Wikipedia ล่ม
_STATIC_FALLBACK: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "AVGO", "META", "GOOGL", "GOOG", "TSLA", "COST",
    "NFLX", "TMUS", "CSCO", "PEP", "ADBE", "AMD", "LIN", "TXN", "QCOM", "INTU",
    "AMGN", "ISRG", "BKNG", "HON", "CMCSA", "AMAT", "PANW", "ADP", "GILD", "VRTX",
    "MU", "ADI", "REGN", "LRCX", "MELI", "PYPL", "SBUX", "KLAC", "SNPS", "CDNS",
    "CRWD", "MAR", "CTAS", "ORLY", "ASML", "CEG", "ABNB", "MRVL", "FTNT", "DASH",
    "ADSK", "WDAY", "NXPI", "PCAR", "ROP", "MNST", "AEP", "PAYX", "CPRT", "FANG",
    "ODFL", "KDP", "ROST", "CHTR", "FAST", "EA", "BKR", "GEHC", "VRSK", "EXC",
    "CTSH", "XEL", "CCEP", "KHC", "DDOG", "LULU", "TTWO", "IDXX", "ZS", "ON",
    "ANSS", "CSGP", "DXCM", "TEAM", "WBD", "MDB", "BIIB", "GFS", "ILMN", "MRNA",
    "WBA", "ARM", "SMCI", "PDD", "ALNY", "APP", "AXON", "PLTR", "MSTR", "LIN",
]


def get_nasdaq100_tickers() -> List[str]:
    return fetch_wiki_tickers(_WIKI_URL, _STATIC_FALLBACK, min_count=80, name="NASDAQ-100")


def get_nasdaq100_static() -> List[str]:
    return list(dict.fromkeys(_STATIC_FALLBACK))
