# Scalping Bot Trade by narisrpd — MT5 Trading Assistant

ตัวช่วยเทรดด้วย MT5 ที่ **เอาสัญญาณจาก Part 1 (CDC Action Zone)** มาประกอบการเทรด
\+ เทคนิคแท่งเทียน/ทรงกราฟ + Risk Management ขั้นสูง → ออก **"ใบสั่งเทรด"** ให้
**คุณกดเองใน MT5** (ไม่ยิงออเดอร์อัตโนมัติ — มือคนคือเบรกสุดท้าย)

## หลักการแยก Part

```
Part 1 (Cloud Run, ไม่แตะ)            Scalping Bot (เครื่อง Windows นี้)
─────────────────────────            ──────────────────────────────
สแกน CDC → ปล่อยสัญญาณ                1. ดึงสัญญาณจาก /signals
ที่ /signals (HTTPS + token)   ─────▶ 2. + แท่งเทียน/ทรงกราฟ (ยืนยัน)
                                      3. + Risk: sizing/R:R/limit
                                      4. ต่อ MT5 (อ่านราคา/สเปก/บัญชี)
                                      5. → ใบสั่งเทรด → Telegram → กดเอง
```
Part 1 **ไม่รู้จัก Scalping Bot** เลย ถ้าลบ Scalping Bot ทิ้ง Part 1 ทำงานปกติ 100%

## ติดตั้ง (ครั้งแรก)

1. ติดตั้ง **MetaTrader 5 terminal** + ล็อกอินบัญชีโบรกให้เรียบร้อย (เปิดค้างไว้)
2. ติดตั้ง Python deps:
   ```
   pip install -r requirements.txt
   ```
3. ก็อป `config.example.env` → `config.env` แล้วเติมค่า:
   - `SIGNALS_TOKEN` = ค่าเดียวกับ `WEBHOOK_SECRET` ของ cdc-bot
   - `MT5_LOGIN / MT5_PASSWORD / MT5_SERVER` = บัญชี MT5
   - `RISK_PCT_PER_TRADE`, `MAX_DAILY_LOSS_PCT`, `MIN_RR` = กฎความเสี่ยง

## ย้ายไป Windows VPS ภายหลัง

ทุกอย่างอ่านจาก `config.env` → ย้ายแค่ **ก็อปโฟลเดอร์ `scalping_bot/` ไป VPS**
ติดตั้ง MT5 + deps + วาง config.env เดิม = ทำงานต่อได้ทันที ไม่ต้องแก้โค้ด

## ข้อจำกัดที่ต้องรู้

- MT5 เทรด **CFD** (ทอง/เงิน/น้ำมัน/ดัชนี/FX ดีสุด · เมกะแคป US ได้ถ้าโบรกมี)
- **หุ้นเล็ก-กลาง** (เช่น ADEA) โบรก MT5 ส่วนใหญ่ไม่มี → ตัวพวกนั้นใช้ Part 1 ดูอย่างเดียว
- ชื่อสัญลักษณ์ต่างกันแต่ละโบรก → ต้อง map ใน `symbol_map` (จะตั้งตอน setup)

## โมดูล (ทยอยเพิ่ม)

- [x] `read_signals.py` — ดึงสัญญาณจาก Part 1
- [ ] `mt5_client.py` — ต่อ MT5, อ่านราคา/สเปก/บัญชี
- [ ] `risk.py` — position sizing + R:R + daily-loss/limit
- [ ] `candles.py` — แท่งเทียน (engulfing/pin bar/doji)
- [ ] `ticket.py` — ประกอบใบสั่งเทรด + ส่ง Telegram
- [ ] `run.py` — loop หลัก (poll → กรอง → ใบสั่ง)
