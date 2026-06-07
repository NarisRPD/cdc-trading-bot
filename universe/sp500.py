"""
universe/sp500.py — ดึงรายชื่อ S&P 500 จาก Wikipedia
มี static fallback ในกรณีที่ scrape ไม่ได้
"""
from __future__ import annotations
import logging
from typing import List

from universe._wiki import fetch_wiki_tickers

log = logging.getLogger(__name__)

_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# Static fallback — snapshot ณ 2025-Q4 (อัปเดตเป็นระยะ)
# ครอบคลุมส่วนใหญ่ที่ liquidity สูง ใช้กันสัญญาณหายเมื่อ Wikipedia ล่ม
_STATIC_FALLBACK: List[str] = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "BRK-B", "AVGO", "TSLA",
    "LLY", "JPM", "V", "WMT", "XOM", "UNH", "MA", "ORCL", "PG", "JNJ",
    "HD", "COST", "ABBV", "BAC", "NFLX", "KO", "CRM", "CVX", "TMUS", "MRK",
    "ADBE", "PEP", "CSCO", "WFC", "AMD", "ACN", "LIN", "ABT", "MCD", "TMO",
    "PM", "GE", "DIS", "IBM", "NOW", "TXN", "ISRG", "VZ", "QCOM", "GS",
    "MS", "DHR", "CAT", "INTU", "AMGN", "BKNG", "PFE", "RTX", "BLK", "SPGI",
    "AXP", "T", "NEE", "LOW", "PGR", "C", "AMAT", "UBER", "HON", "BSX",
    "ETN", "TJX", "ELV", "BX", "SYK", "DE", "VRTX", "ANET", "PLD", "ADP",
    "GILD", "MDT", "LMT", "MMC", "SCHW", "REGN", "ADI", "PANW", "BMY", "MU",
    "FI", "KKR", "CB", "CI", "MO", "PLTR", "INTC", "AMT", "SBUX", "UPS",
    "SO", "DUK", "TT", "ZTS", "ICE", "CDNS", "BA", "GD", "EQIX", "EOG",
    "WM", "AON", "SHW", "MCO", "MDLZ", "CMG", "TGT", "USB", "PYPL", "ITW",
    "SNPS", "PNC", "APH", "CL", "MMM", "FCX", "EMR", "CRWD", "PH", "MSI",
    "ECL", "CSX", "ORLY", "MAR", "FDX", "WELL", "CME", "MCK", "NSC", "TFC",
    "CARR", "PSA", "GM", "ROP", "AJG", "NOC", "AZO", "F", "PAYX", "AEP",
    "AIG", "TDG", "AFL", "TRV", "SLB", "SPG", "MET", "O", "SRE", "ALL",
    "URI", "KMB", "LHX", "AMP", "DLR", "PSX", "BK", "FTNT", "FIS", "DHI",
    "PCAR", "OXY", "ROST", "VLO", "FAST", "MNST", "MPC", "GWW", "PRU", "KMI",
    "HLT", "AME", "NEM", "STZ", "VRSK", "EW", "KR", "CTAS", "DELL", "ABNB",
    "CCI", "PWR", "KDP", "EXC", "CHTR", "ODFL", "GEHC", "PEG", "A", "MSCI",
    "IT", "FANG", "OTIS", "IR", "LEN", "EA", "GIS", "MLM", "CTSH", "WAB",
    "XEL", "RSG", "RCL", "SYY", "VMC", "ED", "DD", "GLW", "WTW", "IDXX",
    "LULU", "AVB", "BKR", "HUM", "EBAY", "DXCM", "ON", "HSY", "MTD", "TROW",
    "BIIB", "WST", "EFX", "GRMN", "FITB", "WEC", "ANSS", "RMD", "STT", "TSCO",
    "DOW", "KEYS", "AWK", "VICI", "HPQ", "EIX", "CHD", "ZBH", "ETR", "DOV",
    "FTV", "WBD", "PPG", "STE", "BR", "PHM", "EQR", "MTB", "ROK", "K",
    "HIG", "NTAP", "TYL", "FOX", "FOXA", "ARE", "WY", "DTE", "GPN", "NDAQ",
    "DG", "LYB", "EQT", "VTR", "GPC", "WAT", "HBAN", "RJF", "IFF", "PPL",
    "VLTO", "ZBRA", "VRSN", "INVH", "EXR", "AEE", "CBOE", "ESS", "MOH", "WBA",
    "ULTA", "TER", "RF", "FE", "MKC", "STX", "ATO", "BRO", "TDY", "PFG",
    "CTRA", "WRB", "CFG", "BLDR", "TSN", "PKG", "CMS", "DRI", "WDC", "CINF",
    "DGX", "PTC", "NTRS", "CLX", "MAA", "OMC", "CCL", "L", "LH", "BAX",
    "BBY", "ALGN", "JBHT", "EXPD", "AVY", "CNC", "AKAM", "POOL", "TXT", "SWK",
    "IP", "HOLX", "WSM", "BG", "DPZ", "EPAM", "SWKS", "NRG", "AMCR", "LDOS",
    "SNA", "FFIV", "CAH", "TRMB", "VTRS", "STLD", "JBL", "CPB", "IRM", "EXPE",
    "DOC", "RVTY", "EVRG", "LNT", "PNR", "BALL", "PODD", "INCY", "LVS", "JNPR",
    "KIM", "MAS", "GEN", "ALB", "TPR", "FDS", "TECH", "TAP", "MGM", "REG",
    "AES", "UDR", "EG", "WYNN", "CRL", "APA", "HST", "ENPH", "CZR", "EMN",
    "JKHY", "AOS", "MOS", "NWSA", "NWS", "MKTX", "PNW", "GL", "NCLH", "WBA",
    "TFX", "QRVO", "BWA", "FRT", "HII", "AAL", "RL", "NWL", "DVA", "BEN",
    "ZION", "DAY", "PARA", "MTCH", "BIO", "CTLT", "WHR", "IVZ", "RHI", "SOLV",
    "DLTR", "FMC", "MHK", "CE", "BIO", "HSIC", "PNR", "VFC", "ROL", "LKQ",
    "AIZ",
]


def get_sp500_static() -> List[str]:
    """static list ไว้เช็ก membership (ไม่ยิง network) — ใช้โดย symbol resolver"""
    return list(dict.fromkeys(_STATIC_FALLBACK))


def get_sp500_tickers() -> List[str]:
    """คืนรายชื่อ S&P 500 (Wikipedia + static fallback, ฟอร์แมต yfinance)"""
    return fetch_wiki_tickers(_WIKI_URL, _STATIC_FALLBACK, min_count=400, name="S&P 500")
