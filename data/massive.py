"""
data/massive.py — Massive (Polygon) option data — ใช้เสริม yahoo เฉพาะ "ราคา premium จริง"
ของ option ที่ถืออยู่ (yahoo ทำไม่ได้)

Free Basic tier: EOD เท่านั้น, 5 calls/นาที, ไม่มี OI/Greeks (พวกนั้นใช้ yahoo)
ต้องตั้ง env MASSIVE_API_KEY (จาก Secret Manager). ถ้าไม่ตั้ง → enabled()=False, ทุกฟังก์ชันคืน None
"""
from __future__ import annotations
import logging
import os
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_BASE = "https://api.polygon.io"
_PREM_CACHE_FILE = "option_premium_cache.json"


def _key() -> str:
    return os.getenv("MASSIVE_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        import requests
        p = dict(params or {})
        p["apiKey"] = _key()
        r = requests.get(f"{_BASE}{path}", params=p, timeout=15)
        if r.status_code != 200:
            log.warning("massive %s → %s %s", path, r.status_code, r.text[:120])
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("massive GET %s failed: %s", path, e)
        return None


def find_contract(underlying: str, opt_type: str, strike: float,
                  target_expiry: Optional[str] = None) -> Optional[dict]:
    """
    หา option contract ที่ strike ตรง + expiry ใกล้ target_expiry สุด (ยังไม่หมดอายุ)
    คืน {"ticker": "O:...", "expiry": "YYYY-MM-DD", "strike": 115.0} หรือ None
    """
    if not enabled():
        return None
    data = _get("/v3/reference/options/contracts", {
        "underlying_ticker": underlying.upper(),
        "contract_type": opt_type if opt_type in ("call", "put") else "put",
        "strike_price": strike,
        "expired": "false",
        "limit": 100,
        "sort": "expiration_date",
    })
    results = (data or {}).get("results") or []
    if not results:
        return None
    if target_expiry:
        try:
            tgt = pd.Timestamp(target_expiry)
            best = min(results, key=lambda c: abs((pd.Timestamp(c["expiration_date"]) - tgt).days))
        except Exception:  # noqa: BLE001
            best = results[0]
    else:
        best = results[0]
    return {"ticker": best["ticker"], "expiry": best["expiration_date"],
            "strike": float(best.get("strike_price", strike))}


def _premium_live(option_ticker: str) -> Optional[float]:
    """ราคา premium EOD ล่าสุด (close) ของ contract — หรือ None"""
    data = _get(f"/v2/aggs/ticker/{option_ticker}/prev", {"adjusted": "true"})
    res = (data or {}).get("results") or []
    if not res or res[0].get("c") is None:
        return None
    return float(res[0]["c"])


# ── premium cache (กัน 5 calls/min limit + ให้ /list เร็ว) ──────────────
def _prem_cache_get(ticker: str, max_age_h: int = 14) -> Optional[float]:
    try:
        from watchlist import store
        e = (store.load_json(_PREM_CACHE_FILE, {}) or {}).get(ticker)
        if not e or e.get("premium") is None:
            return None
        if (pd.Timestamp.now(tz="UTC") - pd.Timestamp(e["ts"])).total_seconds() > max_age_h * 3600:
            return None
        return float(e["premium"])
    except Exception:  # noqa: BLE001
        return None


def prem_cache_put_many(mapping: dict) -> None:
    """เขียน premium cache เป็น batch {ticker: premium}"""
    fresh = {tk: p for tk, p in (mapping or {}).items() if p is not None}
    if not fresh:
        return
    try:
        from watchlist import store
        from datetime import datetime, timezone
        c = store.load_json(_PREM_CACHE_FILE, {}) or {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for tk, p in fresh.items():
            c[tk] = {"premium": round(float(p), 4), "ts": now}
        store.save_json(_PREM_CACHE_FILE, c)
        log.info("premium cache updated: %d contracts", len(fresh))
    except Exception as e:  # noqa: BLE001
        log.warning("premium cache put failed: %s", e)


def premium(option_ticker: str, *, use_cache: bool = True) -> Optional[float]:
    """premium ปัจจุบัน (EOD) — ลอง cache ก่อน แล้วค่อยยิง API (กัน rate limit)"""
    if not enabled() or not option_ticker:
        return None
    if use_cache:
        c = _prem_cache_get(option_ticker)
        if c is not None:
            return c
    p = _premium_live(option_ticker)
    if p is not None:
        prem_cache_put_many({option_ticker: p})
    return p
