"""
universe/sp400.py — รายชื่อ S&P MidCap 400 (Wikipedia + static fallback)

เพิ่มเข้ามาเพื่อให้ /top5 ครอบ "หุ้นขนาดกลาง" ($2B-$20B โดยประมาณ) —
เดิม universe มีแค่ S&P500 (ใหญ่) + NASDAQ100 (เทคใหญ่) + S&P600 (เล็ก)
ช่องว่างตรงกลางคือโซนที่หุ้นเติบโตส่วนใหญ่อาศัยอยู่พอดี
S&P400 มีเกณฑ์กำไรเป็นบวกเหมือน S&P600 → กรองหุ้นปั่น/ขาดทุนเรื้อรังให้ชั้นหนึ่งแล้ว
"""
from __future__ import annotations
from typing import List

from universe._wiki import fetch_wiki_tickers

_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"

# static fallback (subset ~2025) — Wikipedia เป็นหลัก, ตัวนี้แค่กันล่ม
_STATIC_FALLBACK: List[str] = [
    "ACM", "AFG", "AGCO", "ALLY", "AMKR", "ANF", "ARW", "ATR", "AVT", "AYI",
    "BC", "BERY", "BIO", "BJ", "BLD", "BMRN", "BRBR", "BRX", "BURL", "BWXT",
    "CASY", "CBSH", "CC", "CFR", "CGNX", "CHDN", "CHE", "CIEN", "CLF", "CLH",
    "CMC", "CNH", "COHR", "COKE", "CR", "CROX", "CSL", "CUBE", "CW", "DAR",
    "DBX", "DCI", "DKS", "DOCU", "DT", "DUOL", "EGP", "EHC", "ELS", "EME",
    "ENSG", "EQH", "ESAB", "EWBC", "EXEL", "EXP", "FAF", "FCN", "FHI", "FIVE",
    "FLEX", "FLR", "FLS", "FNB", "FND", "GAP", "GGG", "GLPI", "GME", "GNRC",
    "GPK", "GT", "GXO", "HLI", "HQY", "HRB", "HUBB", "ILMN", "INGR", "IPGP",
    "IRT", "ITT", "JAZZ", "JEF", "JLL", "KBH", "KBR", "KEX", "KNX", "LAD",
    "LECO", "LII", "LNTH", "LOPE", "LSCC", "LSTR", "MANH", "MASI", "MEDP", "MIDD",
    "MKSI", "MORN", "MSA", "MTZ", "MUSA", "NBIX", "NVT", "NYT", "OC", "OGE",
    "OHI", "OLED", "ONTO", "ORI", "OSK", "OVV", "PB", "PEN", "PFGC", "PNFP",
    "POST", "PSTG", "R", "RBA", "RBC", "REXR", "RGA", "RGEN", "RGLD", "RNR",
    "RPM", "RRX", "RS", "SAIA", "SCI", "SEIC", "SF", "SFM", "SNX", "SSD",
    "STAG", "SWX", "TXRH", "THO", "TOL", "TPX", "TREX", "TTC", "TTEK", "UFPI",
    "UGI", "UHS", "UNM", "USFD", "VMI", "VNO", "VOYA", "WAL", "WCC", "WEX",
    "WH", "WING", "WLK", "WSO", "WTRG", "WWD", "X", "XPO", "ZI",
]


def get_sp400_tickers() -> List[str]:
    return fetch_wiki_tickers(_WIKI_URL, _STATIC_FALLBACK, min_count=200, name="S&P400")


def get_sp400_static() -> List[str]:
    return list(_STATIC_FALLBACK)
