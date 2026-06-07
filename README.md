# CDC Action Zone V3 — Market Scanner Bot

ระบบสแกนสัญญาณ **CDC Action Zone V3 (โดย piriya33)** บน Daily timeframe
ครอบคลุม **Crypto / US Stocks (S&P 500 + NASDAQ-100 + S&P SmallCap 600 ≈ 1,100 ตัว) / Thai Stocks (SET100) / Commodities**
> US universe ดึงสดจาก Wikipedia (`universe/_wiki.py` ใส่ User-Agent กัน 403) + static fallback · S&P 600 = หุ้นเล็กที่มีเกณฑ์ "กำไรเป็นบวก"
มัดรวมเป็นข้อความเดียวต่อกลุ่ม → ส่ง **Telegram**

---

## โครงสร้าง

```
cdc-scanner/
├── main.py                  # orchestrator (Job) + watchlist report
├── bot.py                   # Telegram watchlist webhook (Service)
├── config.py                # env + toggles
├── core/
│   ├── indicators.py        # EMA / RSI / ADX / SMA (pure pandas)
│   ├── signals.py           # CDC V3 zones + confluence score
│   └── symbols.py           # resolve symbol → market (alias โลหะ, เดาตลาด)
├── data/
│   ├── crypto.py            # ccxt: binance default, bybit/okx fallback
│   ├── stocks.py            # yfinance batch + Stooq fallback (circuit breaker)
│   ├── commodities.py       # yfinance GC=F / SI=F / HG=F
│   └── quote.py             # ดึงข้อมูลรายตัว (watchlist)
├── universe/
│   ├── sp500.py             # Wikipedia + static fallback
│   └── set100.py            # static list liquid ~44 ตัว (อัปเดต 2025-12-01)
├── watchlist/
│   ├── store.py             # positions.json บน GCS (+ local fallback)
│   └── tracker.py           # entry snapshot / P&L / exit alert
├── notify/
│   └── telegram.py          # chunked sender (กัน 4096 limit)
├── requirements.txt
├── Dockerfile               # multi-stage python:3.12-slim
└── .dockerignore
```

---

## 1. นิยามสัญญาณ CDC V3 (สรุปสั้น)

```python
fast = EMA(close, 12)
slow = EMA(close, 26)

bull = fast > slow
green  = bull and close >  fast    # Buy zone
blue   = bull and close <= fast    # Pre-Buy
yellow = (not bull) and close >  fast  # Pre-Sell
red    = (not bull) and close <= fast  # Sell zone
```

- 🟢 **Buy** = แท่งล่าสุดอยู่ `green` และแท่งก่อนหน้า**ไม่ใช่** `green`
- 🔴 **Sell** = แท่งล่าสุดอยู่ `red` และแท่งก่อนหน้า**ไม่ใช่** `red`

**No-repaint:** ใช้แท่งที่ปิดสมบูรณ์แล้วเท่านั้น (`crypto`, `yfinance` ตัดแท่งวันนี้ที่ยังก่อตัวออก)

---

## 2. Confluence Score (0–4) — non-destructive

ทุกสัญญาณยังถูกรายงาน แต่ติดดาวคุณภาพ:

| Filter | Buy | Sell |
|---|---|---|
| Trend | `close > EMA200` | `close < EMA200` |
| Momentum | `ADX > 20` | `ADX > 20` |
| Volume | `volume > SMA(volume, 20)` | เช่นกัน |
| ไม่ overbought/oversold | `RSI < 70` | `RSI > 30` |

- `HIGH-QUALITY` = score ≥ 3 **และ** ผ่าน Trend filter
- **`ALERT_HQ_ONLY=true` (default)** — แจ้งเฉพาะ HIGH-QUALITY (ตัวน่าเข้าจริง) ทั้ง /scan และรายวัน · ตั้ง `false` เพื่อดูทุกสัญญาณ (ใช้ `MIN_SCORE_TO_ALERT` คุมระดับแทน)
- **Buy / Sell แยกเป็นคนละข้อความ** (1 ข้อความต่อกลุ่มต่อทิศ) — อ่านง่าย ไม่ปนกัน
- **🔥 เด็ดสุด** — สัญญาณที่ HIGH-QUALITY **และยืนยันเทรนด์รายสัปดาห์** จะติดป้าย 🔥 + ดันขึ้นบนสุดของข้อความ (จัดลำดับให้ดูตัวเด็ดก่อน)
- **Filter breakdown** (`SHOW_FILTER_BREAKDOWN=true`) — โชว์ใต้สัญญาณว่าผ่าน filter ตัวไหน (`✓ trend>EMA200 · ADX=28 · …`)
- **Multi-timeframe** (`ENABLE_MTF=true`) — เช็กเทรนด์รายสัปดาห์ (EMA12/26 บน weekly) → badge `📈wk✓` (ตรง) / `⚠️wk✗` (สวน) · ตั้ง `REQUIRE_MTF=true` เพื่อส่งเฉพาะสัญญาณที่ตรง weekly

---

## 3. ตัวอย่างข้อความ Telegram

```
🟢 CDC Buy — US Stocks (3 ตัว)
• DELL ⭐⭐⭐⭐ (4/4) HIGH-QUALITY
• PLTR ⭐⭐⭐ (3/4) HIGH-QUALITY
• ROKU ⭐⭐ (2/4)

🔴 CDC Sell — Crypto (1 ตัว)
• SOL/USDT ⭐⭐⭐ (3/4)

📅 อ้างอิงแท่งปิดวันที่: 2026-05-29
```

---

## 4. Environment Variables

| Key | Default | คำอธิบาย |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (required) | จาก @BotFather |
| `TELEGRAM_CHAT_ID` | (required) | จาก getUpdates |
| `CRYPTO_EXCHANGE` | `binance` | `binance` / `bybit` / `okx` |
| `CRYPTO_TOP_N` | `50` | top N by 24h quote volume |
| `ENABLE_FILTERS` | `true` | คำนวณ confluence score |
| `MIN_SCORE_TO_ALERT` | `0` | 0 = รายงานทุกสัญญาณ |
| `ENABLE_EMA200_FILTER` | `true` | คำนวณ EMA200 (ต้องการ ≥200 แท่ง) |
| `MIN_BARS_REQUIRED` | `60` | ขั้นต่ำเผื่อ ENABLE_EMA200_FILTER=false |
| `DRY_RUN` | `false` | ไม่ส่ง Telegram แค่ log |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` |

---

## 5. รันบนเครื่อง (ทดสอบเร็ว ๆ)

```bash
# 1. venv
python -m venv .venv
. .venv/Scripts/activate   # Windows: PowerShell — use `.venv\Scripts\Activate.ps1`
pip install -r requirements.txt

# 2. ตั้ง env แล้วทดสอบแบบ dry-run (ไม่ส่ง Telegram จริง)
$env:TELEGRAM_BOT_TOKEN = "test"   # PowerShell
$env:TELEGRAM_CHAT_ID   = "test"
$env:DRY_RUN            = "true"
$env:CRYPTO_EXCHANGE    = "bybit"  # ถ้าอยู่ในไทย/VPS ที่ Binance ใช้ได้ใส่ binance

python main.py
```

---

## 6. Deploy บน **Google Cloud Run Job** (ฟรี 100% ถ้าทำตามเงื่อนไข)

### 6.1 ที่ต้องเตรียม "ด้วยมือ" ก่อน
1. สมัคร / ล็อกอิน Google Cloud (ผูกบัตร แต่อยู่ในโควตาฟรี = $0)
2. สร้าง project: `cdc-action-zone-alert` (หรือชื่ออื่น)
3. เปิด APIs: **Cloud Run, Cloud Build, Artifact Registry, Cloud Scheduler, Secret Manager**
4. ติดตั้ง `gcloud` CLI → `gcloud auth login`
5. สร้าง Artifact Registry repo:
   ```bash
   gcloud artifacts repositories create cdc \
     --repository-format=docker \
     --location=asia-southeast1
   ```
6. เก็บ secret:
   ```bash
   echo -n "<BOT_TOKEN>" | gcloud secrets create tg-token --data-file=-
   echo -n "<CHAT_ID>"   | gcloud secrets create tg-chat  --data-file=-
   ```

### 6.2 Build + push image
```bash
gcloud config set project cdc-action-zone-alert
gcloud config set run/region asia-southeast1

gcloud builds submit \
  --tag asia-southeast1-docker.pkg.dev/cdc-action-zone-alert/cdc/scanner
```

**ตรวจขนาด image:** ใน Artifact Registry ต้อง **< 0.5GB** (ไม่งั้นเสียค่าเก็บ)

### 6.3 สร้าง Cloud Run Job
```bash
gcloud run jobs create cdc-scanner \
  --image asia-southeast1-docker.pkg.dev/cdc-action-zone-alert/cdc/scanner \
  --region asia-southeast1 \
  --cpu 1 --memory 1Gi \
  --task-timeout 1800 \
  --max-retries 1 \
  --set-env-vars CRYPTO_EXCHANGE=binance,ENABLE_FILTERS=true,MIN_SCORE_TO_ALERT=0 \
  --set-secrets TELEGRAM_BOT_TOKEN=tg-token:latest,TELEGRAM_CHAT_ID=tg-chat:latest
```

### 6.4 ทดสอบรันมือ
```bash
gcloud run jobs execute cdc-scanner --region asia-southeast1 --wait
```
- ดูข้อความเด้งเข้า Telegram
- เช็ก Cloud Logging ว่าไม่มี `HTTP 451` จาก Binance — ถ้าเจอ ให้ update:
  ```bash
  gcloud run jobs update cdc-scanner \
    --region asia-southeast1 \
    --update-env-vars CRYPTO_EXCHANGE=bybit
  ```

### 6.5 ตั้งเวลาด้วย Cloud Scheduler
สร้าง service account ที่มีสิทธิ์ invoke job:
```bash
# (ถ้ายังไม่มี) สร้าง SA
gcloud iam service-accounts create scheduler-invoker \
  --display-name="Cloud Scheduler invoker"

# ให้สิทธิ์ run.invoker
SA="scheduler-invoker@cdc-action-zone-alert.iam.gserviceaccount.com"
gcloud projects add-iam-policy-binding cdc-action-zone-alert \
  --member="serviceAccount:${SA}" \
  --role="roles/run.invoker"
```

สร้าง scheduler:
```bash
PROJECT=cdc-action-zone-alert
gcloud scheduler jobs create http cdc-scanner-trigger \
  --location asia-southeast1 \
  --schedule "30 8 * * *" \
  --time-zone "Asia/Bangkok" \
  --uri "https://asia-southeast1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT}/jobs/cdc-scanner:run" \
  --http-method POST \
  --oauth-service-account-email "${SA}"
```

> `30 8 * * *` Asia/Bangkok = 08:30 น. ไทย — ทุกตลาดปิดแท่งของวันก่อนหน้าครบ
> (อย่าใช้ `30 1` + Asia/Bangkok = ตี 1:30 ตลาด US ยังไม่ปิด จะพลาดแท่งล่าสุด)

### 6.6 รักษาให้ฟรี 100% — checklist
- ✅ image < 0.5GB (multi-stage + slim)
- ✅ รันวันละครั้ง ~5–10 นาที → อยู่ในโควตา Cloud Run free
- ✅ 1 scheduler / 2 secrets → อยู่ในโควตาฟรี
- ⚠️ **ตั้ง Budget Alert ที่ $1** (Billing → Budgets & alerts)

---

## 7. Watchlist Bot (Telegram) — โต้ตอบได้

บอตเสริมสำหรับบันทึก/ติดตาม position ที่ถืออยู่ รันเป็น **Cloud Run Service `cdc-bot`**
(image เดียวกับ scanner, override entrypoint เป็น `gunicorn ... bot:app`, scale-to-zero = ~ฟรี)

### 7.1 คำสั่ง
| เปิด | ปิด (เอาออกจาก watchlist) | ประเภท |
|---|---|---|
| `/buy SYM [ราคา]` | `/sell SYM` | Spot |
| `/callbuy SYM [strike] [ราคาหุ้น]` | `/callsell SYM` | Call (เก็งขึ้น) |
| `/putbuy SYM [strike] [ราคาหุ้น]` | `/putsell SYM` | Put (เก็งลง) |

> **Option (Call/Put) แสดงต่างจาก Spot:** ใส่ strike + ราคาหุ้นอ้างอิง · บอกเป็น **"thesis หุ้นอ้างอิง"** (เป้า/จุด thesis เสีย บนราคาหุ้น ไม่ใช่ premium) + **งบ premium ที่ยอมเสีย** (= ขาดทุนสูงสุดของ long option) แทนจำนวนหุ้น · บอตไม่มีข้อมูล option chain/Greeks → ติดตามได้แค่ "ทิศหุ้น" ไม่ใช่ราคา option จริง

จัดการ: `/list` (ราคา + %P/L + โซน CDC) · `/edit SYM ราคา` · `/note SYM ข้อความ` · `/help`

สแกนทันที: `/scan` = ทั้ง 4 กลุ่ม · `/scan crypto|usstocks|thaistocks|commodity` = เฉพาะหมวด (บอท trigger Job + ส่ง env override `SCAN_GROUPS` เฉพาะ execution นั้น → ผลส่งเข้า Telegram ใน ~1-2 นาที)

สแกนตัวเดียว (ตอบทันทีในบอต ไม่ผ่าน Job): `/scan AAPL` · `/scan BTC` · `/scan PTT.BK` · `/scan XAUUSD` — ดึงข้อมูลตัวนั้นแล้วคำนวณ CDC ตรงนั้นเลย โชว์ **โซนปัจจุบัน (6 สี) เสมอ** + (ถ้ามีสัญญาณใหม่) ดาว/HQ/เป้า/⏱️ หรือ (ถ้ายังไม่มี) อินดิเคเตอร์ EMA200/ADX/RSI/Volume + บทอ่านโซน. รองรับ**หุ้นนอก universe** — บอตเดาตลาดให้ และถ้าเดาเป็น crypto แต่ไม่เจอจะลองเป็นหุ้น US ให้; บังคับตลาดเองได้: `/scan us SOFI` · `/scan thai XYZ`

สถิติ: `/stats` (win-rate, ค่าเฉลี่ย R = expectancy, R รวม)

> **Trade journal:** ทุกครั้งที่ /sell บอตบันทึกไม้ (entry/exit/กำไรเป็น R) ลง `journal.json` → `/stats` คำนวณสถิติ (R-multiple = กำไร% ÷ ระยะ SL เดิม%)
> (เอา position sizing / `/capital` ออกแล้ว — โบรกจัดการ premium/ขนาดไม้/Greeks ของ option ได้ดีกว่า บอตโฟกัสที่ thesis ของหุ้นอ้างอิง)

> **การเตือนปิด:** ถ้าถือ Long/Spot แล้วเข้าโซนขาย (red) หรือถือ Short แล้วเข้าโซนซื้อ (green) จะขึ้น ⚠️ พิจารณาปิด — **เตือนทุกวันที่ยังอยู่โซนนั้น** (persistent กันพลาด) เห็นตอน report 07:00 หรือสั่ง `/scan` เอง · CDC เป็นสัญญาณแท่งปิดรายวัน จึงเตือนหลังแท่งปิด ไม่ใช่ intraday

> **TP/SL อัตโนมัติ (2 โหมด ผ่าน env `RISK_MODE`):**
> - `safe` (default) — SL อิงโครงสร้าง: Long = `min(swing_low(10) − 0.5ATR, เข้า − 2ATR)` (เอาตัวไกลกว่า = ปลอดภัยจาก noise); Short กลับด้าน · TP ที่ R:R 1:1/2:1/3:1 + แนะนำ "ถึง TP1 ปิดบางส่วน + เลื่อน SL มาทุน (breakeven)"
> - `standard` — SL = `ATR_MULT × ATR` (default 2×) ล้วน ๆ
> ATR ปรับตามความผันผวนของแต่ละสินทรัพย์ (crypto/ทอง/หุ้นไม่เท่ากัน) · เก็บลง position + เตือน 🛑 หลุด SL / 🎯 ถึง TP เมื่อราคาแตะ (ดูใน `/list` + report เช้า) · ปรับ `ATR_MULT`, `SWING_LOOKBACK`, `RISK_MODE` ได้

> **คาดคะเนเวลาถึงเป้า + วันหมดอายุ Option (รายตัว):** แต่ละสัญญาณคำนวณ "เวลาน่าจะถึงเป้า" จาก ATR (ระยะ) + ADX (ความแรงเทรนด์) → `⏱️ คาดถึงเป้า ~8-16 วัน → Option หมดอายุ ≥ 29/06 (~29 วัน)` · เทรนด์แรง = ถึงเร็ว/หมดอายุสั้น, เทรนด์อ่อน = นานกว่า · เป็น **คาดคะเนเชิงสถิติ** ไม่ใช่คำทำนายแน่นอน (spot ไม่มี) · logic: `time_to_target_hint()` ใน tracker.py

> **Trailing stop + breakeven (อัตโนมัติ, ทำตอน full scan):** กำไรถึง +1R → เลื่อน SL มาทุน (breakeven, ไม้ไม่เสี่ยงต่อ); +2R → เลื่อนมา +1R; +3R → +2R … (แน่นขึ้นเท่านั้น) บันทึก SL ใหม่ลง GCS + แจ้ง 🔒 ใน report · ปิดได้ด้วย env `TRAIL_ENABLED=false` · ทำเฉพาะ `/scan` เต็ม + 07:00 (ไม่ทำตอน `/scan <หมวด>`)

> **แจ้งเตือนโซนเปลี่ยน near-realtime:** Job แยก `cdc-watchlist` (env `WATCHLIST_ALERT_ONLY=true`) เช็กเฉพาะตัวใน /list → เตือนทันทีเมื่อโซน CDC เปลี่ยน (เทียบ `last_zone`, กันซ้ำ) · ⚠️ thesis เสีย / ✅ เข้าทาง + snapshot โซนทุกตัว · Scheduler: 16:45 (หลังหุ้นไทยปิด) + 04:30 (หลัง US/โลหะปิด) ไทย · 07:00 = สแกนเต็ม backstop · โซนโชว์เป็น emoji (🟢🔵🟡🔴) ใน /list + report ทุกที่

> ⚠️ **ต้องตั้ง `WATCHLIST_BUCKET` ทั้ง Service และ Job** — ถ้า Job ไม่มี จะอ่าน watchlist ไม่เจอ (รายงาน/trailing เงียบ): `gcloud run jobs update cdc-scanner --update-env-vars WATCHLIST_BUCKET=cdc-action-zone-alert-watchlist`

ไม่ใส่ราคา = บอตดึงราคาตลาดล่าสุดให้ · กำไร/ขาดทุน: Spot/Long ขึ้น=บวก, Short ลง=บวก

### 7.2 สัญลักษณ์ (เดา market อัตโนมัติ)
`SOL`→crypto (เติม /USDT) · `AAPL`→US · `CPALL`→ไทย (.BK) · `XAUUSD`/`XAGUSD`/`XCUUSD`→ทอง/เงิน/ทองแดง

### 7.3 สถาปัตยกรรม + security
- เก็บ positions ใน **GCS** `positions.json` (bucket `cdc-action-zone-alert-watchlist`)
- scanner รายวันอ่าน watchlist → แถมหัวข้อ "📊 สถานะ Watchlist" + เตือน ⚠️ เมื่อตัวที่ถือเจอสัญญาณตรงข้าม (ไม่ลบให้เอง)
- **ปลอดภัย:** Telegram secret_token header (secret `tg-webhook-secret`) + ตอบเฉพาะ `TELEGRAM_CHAT_ID` ของเจ้าของ

### 7.4 deploy / ตั้ง webhook
```bash
# bucket + webhook secret + สิทธิ์ (ทำครั้งเดียว)
gcloud storage buckets create gs://cdc-action-zone-alert-watchlist --location=asia-southeast1 --uniform-bucket-level-access
gcloud secrets create tg-webhook-secret --data-file=- <<< "$(openssl rand -hex 32)"
SA=220154687132-compute@developer.gserviceaccount.com
gcloud storage buckets add-iam-policy-binding gs://cdc-action-zone-alert-watchlist --member=serviceAccount:$SA --role=roles/storage.objectAdmin
gcloud secrets add-iam-policy-binding tg-webhook-secret --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor

# deploy Service (ต้อง --allow-unauthenticated เพื่อให้ Telegram POST เข้าได้)
gcloud run deploy cdc-bot --image asia-southeast1-docker.pkg.dev/cdc-action-zone-alert/cdc/scanner \
  --region asia-southeast1 --command gunicorn \
  --args=--bind=:8080,--workers=1,--threads=4,--timeout=120,bot:app \
  --cpu 1 --memory 1Gi --min-instances 0 --max-instances 1 --allow-unauthenticated \
  --set-env-vars CRYPTO_EXCHANGE=binance,WATCHLIST_BUCKET=cdc-action-zone-alert-watchlist \
  --set-secrets TELEGRAM_BOT_TOKEN=tg-token:latest,TELEGRAM_CHAT_ID=tg-chat:latest,WEBHOOK_SECRET=tg-webhook-secret:latest

# ผูก webhook กับ Telegram (URL = Service URL + /webhook)
TOKEN=$(gcloud secrets versions access latest --secret=tg-token)
SECRET=$(gcloud secrets versions access latest --secret=tg-webhook-secret)
URL=$(gcloud run services describe cdc-bot --region asia-southeast1 --format='value(status.url)')
curl -s "https://api.telegram.org/bot$TOKEN/setWebhook" -d "url=$URL/webhook" -d "secret_token=$SECRET"
```
> แก้โค้ดบอตภายหลัง: rebuild image แล้ว `gcloud run services update cdc-bot --image ...` (image-only ไม่ต้องตั้ง webhook ใหม่)

**ตั้งเมนูคำสั่ง (เด้งตอนพิมพ์ "/" — แยกจากข้อความ /help, ทำครั้งเดียว/เมื่อเพิ่มคำสั่งใหม่):**
```bash
TOKEN=$(gcloud secrets versions access latest --secret=tg-token)
curl -s "https://api.telegram.org/bot$TOKEN/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{"commands":[{"command":"buy","description":"ซื้อ Spot"},{"command":"longbuy","description":"เปิด Long"},{"command":"shortbuy","description":"เปิด Short"},{"command":"sell","description":"ปิด Spot"},{"command":"longsell","description":"ปิด Long"},{"command":"shortsell","description":"ปิด Short"},{"command":"list","description":"ดูพอร์ต"},{"command":"scan","description":"สแกนสัญญาณ"},{"command":"edit","description":"แก้ราคาเข้า"},{"command":"note","description":"ใส่โน้ต"},{"command":"help","description":"วิธีใช้"}]}'
```

---

## 8. หมายเหตุสำคัญ

- **Thai stocks (`.BK`)** ข้อมูลพังบ่อยสุด — ตัวที่ NaN/แถวไม่พอ ระบบจะข้ามและ log ชื่อ ไม่ทำให้ทั้งกลุ่มล่ม
- **yfinance ต้องเป็น 1.x** (0.2.x ใช้ไม่ได้แล้ว — Yahoo เปลี่ยน API ต้องใช้ curl_cffi + pandas 3.x)
- **`MIN_SCORE_TO_ALERT`** ปรับระดับ noise: `0`=ทุกสัญญาณ, `2`=ค่าที่ใช้อยู่ (≥2 ดาว เข้าต้นรอบ), `3`=เฉพาะคุณภาพสูง
- **image hygiene:** rebuild หลายรอบทำ untagged version สะสมจนเกิน 0.5GB ได้ — มี cleanup policy บน repo `cdc` กันไว้แล้ว
- บอตนี้แจ้งเตือน/บันทึกอย่างเดียว — **ห้ามต่อยอดเป็น auto-trade เงินจริง**
