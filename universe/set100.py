"""
universe/set100.py — รายชื่อหุ้นไทยที่สแกน

โฟกัส: **แกน SET50 ~44 ตัว** ที่ liquidity สูงสุด — เลือกเพราะ
1. สัญญาณ CDC ชัดเจนกว่า (large-cap เทรนด์ลื่น whipsaw น้อย)
2. yfinance `.BK` มีข้อมูลครบกว่าหุ้นเล็ก (ลดการถูก skip)

ปรับได้ตรง ๆ: อยากเพิ่มหุ้นที่ตัวเองเทรด ก็เติม symbol (ไม่ต้องใส่ .BK)
SET ทบทวนดัชนีทุก 6 เดือน — อัปเดตล่าสุด: 2025-12-01

หมายเหตุ: ใช้กับ yfinance ต่อท้ายด้วย ".BK"
"""
from __future__ import annotations
from typing import List


_SET100: List[str] = [
    # Energy / Utilities
    "PTT", "PTTEP", "PTTGC", "TOP", "GULF", "GPSC", "BGRIM", "BANPU",
    # Banking
    "KBANK", "SCB", "BBL", "KTB", "TTB",
    # Finance
    "MTC", "SAWAD", "TIDLOR",
    # Telecom
    "ADVANC", "TRUE", "INTUCH",
    # Commerce / Retail
    "CPALL", "CPAXT", "CPN", "CRC", "BJC", "HMPRO", "COM7",
    # Food / Beverage
    "CPF", "TU", "MINT", "OSP", "CBG",
    # Healthcare
    "BDMS", "BH",
    # Materials / Electronics
    "SCC", "SCGP", "DELTA", "HANA",
    # Property / Infrastructure / Transport
    "LH", "AP", "WHA", "AOT", "BTS", "BEM",
    # Tourism
    "CENTEL",
    # ─── ส่วนขยายให้ครบ ~SET100 (mid-cap liquidity รองลงมา) ───
    # Energy / Utilities
    "EGCO", "RATCH", "EA", "BCP", "IRPC", "SPRC", "CKP", "BPP", "TPIPP", "GUNKUL",
    # Banking / Finance
    "TISCO", "KKP", "KTC", "JMT", "JMART", "BAM", "SINGER", "TIDLOR",
    # Property
    "SPALI", "QH", "SIRI", "ORI", "AMATA", "WHAUP", "ANAN", "PSH",
    # Commerce
    "GLOBAL", "DOHOME", "ILM", "BEYOND",
    # Food / Beverage / Agri
    "GFPT", "TFG", "M", "ICHI", "SAPPE", "STA", "NER", "RBF",
    # Healthcare
    "BCH", "CHG", "RJH", "THG", "PR9",
    # Industrial / Materials
    "STGT", "TASCO", "EPG", "TPIPL", "SVI", "KCE",
    # Transport / Marine
    "PSL", "RCL", "TTA", "III",
    # Tourism / Media
    "ERW", "AWC", "WORK", "BEC", "MAJOR", "PLANB", "VGI",
    # Construction
    "STEC", "CK", "ITD",
]


def get_set100_tickers() -> List[str]:
    """คืน symbol .BK พร้อมใช้กับ yfinance"""
    return [f"{s}.BK" for s in dict.fromkeys(_SET100)]


def get_set100_symbols() -> List[str]:
    """คืน symbol ดิบ (ไม่มี .BK) ไว้เช็ก membership — ใช้โดย symbol resolver"""
    return list(dict.fromkeys(_SET100))


def strip_bk_suffix(symbol: str) -> str:
    """แปลง 'CPALL.BK' → 'CPALL' สำหรับโชว์ในข้อความ"""
    return symbol[:-3] if symbol.endswith(".BK") else symbol
