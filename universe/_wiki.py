"""
universe/_wiki.py — ดึงรายชื่อ ticker จาก Wikipedia (ใส่ User-Agent กัน HTTP 403)
ใช้ร่วมกันโดย sp500 / nasdaq100 / sp600
"""
from __future__ import annotations
import io
import logging
from typing import List

import pandas as pd
import requests

log = logging.getLogger(__name__)

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_wiki_tickers(
    url: str,
    static_fallback: List[str],
    *,
    min_count: int = 50,
    name: str = "",
) -> List[str]:
    """
    ดึง ticker จากตารางในหน้า Wikipedia (มองหา column 'Symbol'/'Ticker')
    คืน static_fallback ถ้าดึงไม่ได้/ได้น้อยเกิน — ฟอร์แมต yfinance ('.'→'-')
    """
    label = name or url
    try:
        r = requests.get(url, headers=_UA, timeout=25)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
        for df in tables:
            col = next(
                (c for c in df.columns if str(c).strip().lower() in ("symbol", "ticker")),
                None,
            )
            if col is None:
                continue
            tickers = (
                df[col].astype(str).str.strip().str.replace(".", "-", regex=False).tolist()
            )
            tickers = [
                t for t in tickers
                if t and t.isascii() and 1 <= len(t) <= 8 and t.replace("-", "").isalnum()
            ]
            tickers = list(dict.fromkeys(tickers))
            if len(tickers) >= min_count:
                log.info("%s: โหลดจาก Wikipedia %d ตัว", label, len(tickers))
                return tickers
        raise RuntimeError("ไม่พบตาราง ticker ที่เหมาะสม")
    except Exception as e:  # noqa: BLE001
        log.warning("ดึง %s ไม่สำเร็จ (%s) — ใช้ static fallback", label, e)
        return list(dict.fromkeys(static_fallback))
