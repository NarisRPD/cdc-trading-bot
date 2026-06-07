"""
data/fundamentals.py — มิติ "พื้นฐาน + นักวิเคราะห์ + ข่าว + insider + มาโคร"
เสริม CDC (technical) ให้ลงทุนมั่นใจขึ้น — US เท่านั้น

แหล่งข้อมูล (free tier):
- Finnhub: analyst rating · earnings surprise · ข่าว · insider · ROE/growth · economic calendar
- FMP (stable): net margin · debt/EBITDA · current ratio (เสริมงบ)

ต้องตั้ง env FINNHUB_API_KEY (+ FMP_API_KEY ถ้าจะใช้งบลึก). cache ลง GCS กัน rate limit
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)

_FH_BASE = "https://finnhub.io/api/v1"
_FMP_BASE = "https://financialmodelingprep.com/stable"
_CACHE_FILE = "fundamentals_cache.json"
_MACRO_FILE = "macro_cache.json"
_TTL_H = 12  # งบ/analyst เปลี่ยนช้า → cache 12 ชม.


def _fh_key() -> str:
    return os.getenv("FINNHUB_API_KEY", "").strip()


def _fmp_key() -> str:
    return os.getenv("FMP_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_fh_key())


def _get(url: str, params: dict) -> Optional[object]:
    try:
        import requests
        r = requests.get(url, params=params, timeout=12)
        if r.status_code != 200:
            log.warning("fundamentals %s → %s %s", url.rsplit("/", 1)[-1], r.status_code, r.text[:80])
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("fundamentals GET failed: %s", e)
        return None


def _fh(path: str, params: dict) -> Optional[object]:
    return _get(f"{_FH_BASE}{path}", {**params, "token": _fh_key()})


def _fmp(path: str, params: dict) -> Optional[object]:
    if not _fmp_key():
        return None
    return _get(f"{_FMP_BASE}{path}", {**params, "apikey": _fmp_key()})


# ── cache (per-symbol, รวมทุก field) ──────────────────────────────────
def _cache_get(symbol: str) -> Optional[dict]:
    try:
        from watchlist import store
        import pandas as pd
        e = (store.load_json(_CACHE_FILE, {}) or {}).get(symbol.upper())
        if not e or not e.get("ts"):
            return None
        if (pd.Timestamp.now(tz="UTC") - pd.Timestamp(e["ts"])).total_seconds() > _TTL_H * 3600:
            return None
        return e
    except Exception:  # noqa: BLE001
        return None


def _cache_put(symbol: str, data: dict) -> None:
    try:
        from watchlist import store
        c = store.load_json(_CACHE_FILE, {}) or {}
        c[symbol.upper()] = {**data, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        store.save_json(_CACHE_FILE, c)
    except Exception as e:  # noqa: BLE001
        log.warning("fundamentals cache put failed: %s", e)


def _net_insider(rows: list) -> Optional[int]:
    """หุ้นสุทธิที่ผู้บริหารซื้อ-ขาย (code P=ซื้อ, S=ขาย) ใน ~90 วันล่าสุด"""
    if not rows:
        return None
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    net = 0
    found = False
    for r in rows:
        if (r.get("transactionDate") or "") < cutoff:
            continue
        if r.get("transactionCode") in ("P", "S"):
            net += int(r.get("change") or 0)
            found = True
    return net if found else None


def fundamentals(symbol: str) -> dict:
    """ดึง+รวมทุกมิติ (cached 12 ชม.) — US เท่านั้น"""
    cached = _cache_get(symbol)
    if cached is not None:
        return cached
    d: dict = {}
    if enabled():
        rec = _fh("/stock/recommendation", {"symbol": symbol})
        if isinstance(rec, list) and rec:
            a = rec[0]
            d["analyst"] = {k: a.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
        met = _fh("/stock/metric", {"symbol": symbol, "metric": "all"})
        if isinstance(met, dict):
            m = met.get("metric", {}) or {}
            d["roe"] = m.get("roeTTM")
            d["rev_g"] = m.get("revenueGrowthTTMYoy")
            d["eps_g"] = m.get("epsGrowthTTMYoy")
        earn = _fh("/stock/earnings", {"symbol": symbol})
        if isinstance(earn, list) and earn:
            d["last_surprise"] = earn[0].get("surprisePercent")
        ins = _fh("/stock/insider-transactions", {"symbol": symbol})
        if isinstance(ins, dict):
            d["insider_net"] = _net_insider(ins.get("data", []))
        frm = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        to = datetime.now().strftime("%Y-%m-%d")
        news = _fh("/company-news", {"symbol": symbol, "from": frm, "to": to})
        if isinstance(news, list) and news:
            d["news"] = news[0].get("headline")
            d["news_dt"] = news[0].get("datetime")
    # FMP เสริม margin/debt
    rat = _fmp("/ratios-ttm", {"symbol": symbol})
    if isinstance(rat, list) and rat:
        d["net_margin"] = rat[0].get("netProfitMarginTTM")
    km = _fmp("/key-metrics-ttm", {"symbol": symbol})
    if isinstance(km, list) and km:
        d["debt_ebitda"] = km[0].get("netDebtToEBITDATTM")
        d["current_ratio"] = km[0].get("currentRatioTTM")
    _cache_put(symbol, d)
    return d


def news_items(symbol: str, since_epoch: int) -> list[dict]:
    """ข่าวบริษัท (US) ที่ออกหลัง since_epoch (unix วินาที) — คืน list ดิบที่ normalize แล้ว
    ไม่ cache (ต้องสดเพื่อ 'ทันข่าว'). คืน [] ถ้า Finnhub ไม่พร้อม/ไม่มีข่าว/error.
    ดึงตั้งแต่เมื่อวาน→วันนี้ (UTC) เพื่อกันข่าวคาบเส้นเที่ยงคืน แล้วค่อยกรองด้วย timestamp"""
    if not enabled():
        return []
    frm = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = _fh("/company-news", {"symbol": symbol.upper(), "from": frm, "to": to})
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for n in data:
        dt = n.get("datetime")
        if not isinstance(dt, (int, float)) or dt < since_epoch:
            continue
        headline = (n.get("headline") or "").strip()
        if not headline:
            continue
        out.append({
            "id": str(n.get("id") or f"{symbol}:{int(dt)}"),
            "headline": headline,
            "summary": (n.get("summary") or "").strip(),
            "source": (n.get("source") or "").strip(),
            "url": n.get("url") or "",
            "datetime": int(dt),
        })
    return out


_BULL_KW = ("beat", "beats", "surge", "surges", "jump", "jumps", "soar", "soars",
            "rally", "rallies", "upgrade", "upgraded", "raise", "raises", "raised",
            "record", "outperform", "tops", "approval", "approved", "wins", "buyback",
            "strong", "gains", "boost", "rises", "higher", "rebound", "beats estimates")
_BEAR_KW = ("miss", "misses", "missed", "plunge", "plunges", "plummet", "slump",
            "slumps", "downgrade", "downgraded", "cut", "cuts", "lawsuit", "sued",
            "probe", "investigation", "recall", "recalls", "warning", "warns", "weak",
            "halt", "halts", "bankruptcy", "sinks", "falls", "drop", "drops", "lower",
            "loss", "losses", "fraud", "decline", "declines", "slashes", "delay")


def earnings_window(symbol: str, back: int = 3, ahead: int = 7) -> list[dict]:
    """ปฏิทินงบในช่วง [วันนี้-back, วันนี้+ahead] (Finnhub) — คืน list
      แต่ละชิ้น: {date, hour(bmo/amc), epsEstimate, epsActual, revenueEstimate, revenueActual}
      date >= วันนี้ = preview (รอประกาศ) · date < วันนี้ + epsActual != None = recap (ออกแล้ว)
    คืน [] ถ้าไม่พร้อม/ไม่มี"""
    if not enabled():
        return []
    frm = (datetime.now() - timedelta(days=back)).strftime("%Y-%m-%d")
    to = (datetime.now() + timedelta(days=ahead)).strftime("%Y-%m-%d")
    d = _fh("/calendar/earnings", {"symbol": symbol.upper(), "from": frm, "to": to})
    if isinstance(d, dict):
        return d.get("earningsCalendar") or []
    return []


def news_direction(headline: str) -> str:
    """ติดป้ายทิศข่าวจาก keyword (เบา ๆ พอบอกโทน): 📈 บวก / 📉 ลบ / 📰 กลาง"""
    h = (headline or "").lower()
    bull = sum(1 for w in _BULL_KW if w in h)
    bear = sum(1 for w in _BEAR_KW if w in h)
    if bull > bear:
        return "📈"
    if bear > bull:
        return "📉"
    return "📰"


def _analyst_line(a: Optional[dict]) -> Optional[str]:
    if not a:
        return None
    sb, b, h, s, ss = (a.get("strongBuy", 0), a.get("buy", 0), a.get("hold", 0),
                       a.get("sell", 0), a.get("strongSell", 0))
    total = sb + b + h + s + ss
    if total == 0:
        return None
    bull, bear = sb + b, s + ss
    if bull / total >= 0.6:
        v = "ส่วนใหญ่แนะซื้อ ✅"
    elif bear / total >= 0.35:
        v = "ระวัง — ไม่ค่อยเชียร์ ⚠️"
    else:
        v = "กลาง ๆ"
    return f"👔 นักวิเคราะห์: {bull} ซื้อ / {h} ถือ / {bear} ขาย → {v}"


def _quality_parts(d: dict) -> tuple:
    """คืน (รายละเอียด list, verdict, score)"""
    parts, score, n = [], 0, 0
    roe, rev_g, eps_g = d.get("roe"), d.get("rev_g"), d.get("eps_g")
    nm, de, cr = d.get("net_margin"), d.get("debt_ebitda"), d.get("current_ratio")
    if roe is not None:
        n += 1; score += 1 if roe >= 15 else 0
        parts.append(f"ROE {roe:.0f}%")
    if nm is not None:
        n += 1; score += 1 if nm >= 0.10 else 0
        parts.append(f"margin {nm*100:.0f}%")
    if rev_g is not None:
        n += 1; score += 1 if rev_g > 0 else 0
        parts.append(f"รายได้โต {rev_g:+.0f}%")
    if de is not None:
        n += 1; score += 1 if de < 3 else 0
        parts.append(f"หนี้/EBITDA {de:.1f}")
    verdict = ""
    if n >= 2:
        ratio = score / n
        verdict = "แข็งแรง ✅" if ratio >= 0.75 else ("อ่อน ⚠️" if ratio <= 0.4 else "ปานกลาง")
    return parts, verdict, (score, n)


def fundamental_block(symbol: str) -> str:
    """บล็อกเต็มสำหรับ /scan SYMBOL — analyst + งบ + ข่าว + insider + earnings"""
    if not enabled():
        return ""
    d = fundamentals(symbol)
    if not d:
        return ""
    lines = ["🏦 พื้นฐาน & นักวิเคราะห์:"]
    al = _analyst_line(d.get("analyst"))
    if al:
        lines.append(al)
    parts, verdict, _ = _quality_parts(d)
    if parts:
        lines.append(f"📊 งบ: {' · '.join(parts)}" + (f" → {verdict}" if verdict else ""))
    ins = d.get("insider_net")
    if ins:
        if ins > 0:
            lines.append(f"🏛️ ผู้บริหารซื้อสุทธิ {ins:,} หุ้น (90 วัน) ✅")
        elif ins < 0:
            lines.append(f"🏛️ ผู้บริหารขายสุทธิ {abs(ins):,} หุ้น (90 วัน)")
    ls = d.get("last_surprise")
    if ls is not None:
        tag = "เกินคาด ✅" if ls > 0 else ("ต่ำกว่าคาด ⚠️" if ls < 0 else "ตรงคาด")
        lines.append(f"📅 งบล่าสุด: {tag} ({ls:+.1f}%)")
    nw, ndt = d.get("news"), d.get("news_dt")
    if nw:
        age = ""
        try:
            days = (datetime.now() - datetime.fromtimestamp(ndt)).days
            age = f" ({days} วันก่อน)" if days >= 1 else " (วันนี้)"
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"📰 ข่าว: {nw[:90]}{age}")
    return "\n".join(lines) if len(lines) > 1 else ""


def fundamental_flag(symbol: str) -> str:
    """บรรทัดสั้นสำหรับ scan กลุ่ม — analyst + quality verdict"""
    if not enabled():
        return ""
    d = fundamentals(symbol)
    if not d:
        return ""
    bits = []
    a = d.get("analyst")
    if a:
        total = sum(a.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        bull = a.get("strongBuy", 0) + a.get("buy", 0)
        if total:
            mark = "✅" if bull / total >= 0.6 else ("⚠️" if (a.get("sell", 0) + a.get("strongSell", 0)) / total >= 0.35 else "")
            bits.append(f"นักวิเคราะห์เชียร์ซื้อ {bull}/{total} {mark}".rstrip())
    _, verdict, _ = _quality_parts(d)
    if verdict:
        bits.append(f"พื้นฐาน{verdict}")
    return "🏦 " + " · ".join(bits) if bits else ""


# ── economic calendar (มาโคร) ─────────────────────────────────────────
def macro_warning(days: int = 5) -> str:
    """เหตุการณ์มาโคร US ที่กระทบสูง ใน N วันข้างหน้า (cached 6 ชม.)"""
    if not enabled():
        return ""
    try:
        from watchlist import store
        import pandas as pd
        cache = store.load_json(_MACRO_FILE, {}) or {}
        if cache.get("ts") and (pd.Timestamp.now(tz="UTC") - pd.Timestamp(cache["ts"])).total_seconds() < 6 * 3600:
            return cache.get("text", "")
    except Exception:  # noqa: BLE001
        pass
    data = _fh("/calendar/economic", {})
    events = (data or {}).get("economicCalendar", []) if isinstance(data, dict) else []
    today = datetime.now().date()
    horizon = today + timedelta(days=days)
    hot = []
    for e in events:
        if e.get("country") != "US" or e.get("impact") not in ("high", "medium"):
            continue
        try:
            dt = datetime.strptime((e.get("time") or "")[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            continue
        if today <= dt <= horizon:
            mark = "🔴" if e.get("impact") == "high" else "🟡"
            hot.append((dt, f"{mark} {dt.strftime('%d/%m')} {e.get('event')}"))
    hot.sort(key=lambda x: x[0])
    text = ""
    if hot:
        text = "📅 มาโคร US ใกล้นี้ (ระวังผันผวน):\n" + "\n".join(t for _, t in hot[:6])
    try:
        from watchlist import store
        store.save_json(_MACRO_FILE, {"text": text,
                                      "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    except Exception:  # noqa: BLE001
        pass
    return text
