"""
test_connect.py — เช็กว่าต่อ MT5 ได้ไหม + โชว์บัญชี/สัญลักษณ์
รัน:  python test_connect.py
ก่อนรัน: เปิด MetaTrader 5 + ล็อกอินบัญชี (เดโม่หรือจริง) ให้เชื่อมต่อโบรกก่อน
"""
import mt5_client as m
from _config import load

TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
cfg = load()
login = cfg.get("MT5_LOGIN")
pw = cfg.get("MT5_PASSWORD")
server = cfg.get("MT5_SERVER")

# ถ้ามี credential ใน config.env → ล็อกอินตรง (กัน -6 จาก build mismatch)
if login and pw and server:
    ok = m.connect(path=TERMINAL, login=int(login), password=pw, server=server)
else:
    print("ℹ️ ไม่พบ MT5_LOGIN/PASSWORD/SERVER ใน config.env — ลอง attach เฉย ๆ")
    ok = m.connect(path=TERMINAL)

if not ok:
    print("❌ ต่อ MT5 ไม่ได้ — เช็ก login/password/server ใน config.env หรือเปิด terminal+ล็อกอินก่อน")
    raise SystemExit(1)

acc = m.account()
if not acc:
    print("⚠️ ต่อ terminal ได้ แต่ยังไม่ล็อกอินบัญชี — ล็อกอินใน MT5 ก่อน")
    m.shutdown(); raise SystemExit(1)

print("✅ เชื่อมต่อสำเร็จ")
print(f"   บัญชี {acc['login']} @ {acc['server']}")
print(f"   ยอด {acc['balance']:,.2f} {acc['currency']} · leverage 1:{acc['leverage']}")

syms = m.list_symbols()
print(f"\nสัญลักษณ์ที่โบรกมี: {len(syms)} ตัว")
for q in ["XAU", "XAG", "OIL", "US30", "NAS", "SPX", "EURUSD", "BTC", "AAPL", "NVDA"]:
    hit = m.find_symbol(q)
    print(f"   {q:8s} -> {hit[:4] if hit else '(ไม่มี)'}")

# ทดสอบ sizing จริงจากสเปกโบรก (ตัวอย่างทอง)
gold = m.find_symbol("XAU")
if gold:
    df = m.rates(gold[0], "H1", 50)
    if df is not None:
        spot = float(df["close"].iloc[-1])
        lot = m.lots_for_risk(gold[0], acc["balance"], 1.0, spot, spot - 5.0)
        print(f"\nตัวอย่าง {gold[0]} ราคา {spot}: เสี่ยง 1% SL ห่าง 5 → {lot}")

m.shutdown()
