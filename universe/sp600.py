"""
universe/sp600.py — รายชื่อ S&P SmallCap 600 (Wikipedia + static fallback)
S&P 600 มีเกณฑ์ "ต้องกำไรเป็นบวก" ถึงเข้าดัชนี → เหมาะกับ "หุ้นเล็กที่เริ่มมีกำไร"
"""
from __future__ import annotations
from typing import List

from universe._wiki import fetch_wiki_tickers

_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"

# static fallback (subset ~2025) — Wikipedia เป็นหลัก, ตัวนี้แค่กันล่ม
_STATIC_FALLBACK: List[str] = [
    "AAP", "AAT", "ABCB", "ABG", "ABM", "ABR", "ACA", "ACIW", "ADEA", "AEIS",
    "AGYS", "AIN", "AIR", "ALEX", "ALG", "ALGT", "ALKS", "AMN", "AMR", "AMSF",
    "AMWD", "ANDE", "AORT", "APAM", "APLE", "APOG", "ARCB", "AROC", "ASGN", "ASO",
    "ATGE", "AUB", "AVA", "AWR", "AX", "AZZ", "BANF", "BANR", "BCC", "BCPC",
    "BDN", "BFH", "BGS", "BHE", "BJRI", "BKE", "BL", "BMI", "BOH", "BOOT",
    "BRC", "BRKL", "BXMT", "CABO", "CAKE", "CAL", "CALM", "CARG", "CASH", "CATY",
    "CBRL", "CBU", "CCOI", "CENT", "CENX", "CHCO", "CHEF", "CNK", "CNMD", "COLL",
    "CORT", "CPF", "CPK", "CRC", "CRK", "CSGS", "CSR", "CTRE", "CTS", "CVBF",
    "CVCO", "CWT", "DCOM", "DEA", "DFIN", "DIOD", "DNOW", "DORM", "DRH", "DXC",
    "DXPE", "EAT", "ECPG", "EGBN", "EIG", "ELME", "ENR", "ENV", "EPAC", "EPRT",
    "ETD", "EXPI", "EXTR", "EYE", "EZPW", "FBK", "FBNC", "FCF", "FELE", "FFBC",
    "FHB", "FIZZ", "FORM", "FOXF", "FUL", "FULT", "GBX", "GEO", "GFF", "GIII",
    "GMS", "GNW", "GO", "GVA", "HASI", "HAYW", "HCC", "HCI", "HELE", "HI",
    "HLIT", "HMN", "HNI", "HOPE", "HP", "HSII", "HWKN", "HZO", "IBP", "ICUI",
    "IIIN", "INDB", "INVA", "IOSP", "ITGR", "ITRI", "JACK", "JBLU", "JBT", "JJSF",
    "JOE", "JXN", "KAI", "KALU", "KELYA", "KFRC", "KMT", "KN", "KOP", "KREF",
    "KWR", "LCII", "LGND", "LKFN", "LMAT", "LNN", "LPG", "LQDT", "LRN", "LXP",
    "LZB", "MAC", "MARA", "MATX", "MC", "MCY", "MDU", "MGEE", "MGY", "MHO",
    "MLI", "MMI", "MMSI", "MODG", "MOG-A", "MP", "MRTN", "MSGS", "MTH", "MTRN",
    "MTX", "MXL", "MYRG", "NABL", "NARI", "NATL", "NBHC", "NEO", "NGVT", "NHC",
    "NMIH", "NPK", "NPO", "NSIT", "NTCT", "NWBI", "NWN", "NX", "OFG", "OGN",
    "OII", "OMCL", "OSIS", "OUT", "OXM", "PARR", "PATK", "PBH", "PECO", "PFS",
    "PGNY", "PI", "PINC", "PIPR", "PJT", "PLAB", "PLXS", "PMT", "POWL", "PPBI",
    "PRA", "PRDO", "PRGS", "PRK", "PTGX", "PZZA", "QNST", "RAMP", "RC", "RDN",
    "REX", "RGR", "ROG", "RUN", "RUSHA", "RXO", "SABR", "SAFT", "SBCF", "SBSI",
    "SCL", "SCSC", "SEE", "SFNC", "SGH", "SHAK", "SHOO", "SITM", "SKT", "SKYW",
    "SLG", "SM", "SMP", "SMTC", "SNCY", "SNDR", "SONO", "SPNT", "SPSC", "SPTN",
    "SSTK", "STBA", "STC", "STEP", "STRA", "SUPN", "SXC", "SXI", "TALO", "TBBK",
    "TDS", "TDW", "TFIN", "TGNA", "THRM", "THS", "TMP", "TNC", "TNDM", "TPH",
    "TRIP", "TRMK", "TRN", "TRUP", "TTMI", "UCTT", "UE", "UFPT", "UNF", "UNFI",
    "URBN", "USPH", "UVV", "VBTX", "VC", "VCEL", "VECO", "VFC", "VIAV", "VRRM",
    "VSCO", "VSTO", "WABC", "WAFD", "WD", "WDFC", "WEN", "WGO", "WHD", "WKC",
    "WOR", "WS", "WSFS", "WT", "WWW", "XHR", "XNCR", "XPEL", "YELP", "ZD",
]


def get_sp600_tickers() -> List[str]:
    return fetch_wiki_tickers(_WIKI_URL, _STATIC_FALLBACK, min_count=300, name="S&P 600")


def get_sp600_static() -> List[str]:
    return list(dict.fromkeys(_STATIC_FALLBACK))
