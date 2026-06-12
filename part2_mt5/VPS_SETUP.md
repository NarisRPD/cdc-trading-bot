# คู่มือย้ายบอท Scalping Bot ขึ้น Windows VPS (รัน 24/7)

> เป้าหมาย: บอทเทรดเองตลอด ไม่ต้องเปิดคอมตัวเองค้าง · ทนรีบูต/เน็ตหลุด · เสาร์-อาทิตย์ก็เทรด BTC/ETH ต่อ

---

## ⚠️ 0) อ่านก่อน — เศรษฐศาสตร์ (สำคัญที่สุด)
VPS Windows ราคา ~**$8–15/เดือน** · พอร์ตคุณตอนนี้ **$100**
→ VPS กิน **8–15%/เดือน** ของพอร์ต = บอทต้องทำกำไร >15%/เดือนแค่ค่า VPS = **ไม่คุ้ม**

**คำแนะนำเรื่องจังหวะ:**
| พอร์ต | ทำอะไร |
|---|---|
| ตอนนี้ $100 | รันบนคอมตัวเองไปก่อน (ค่าไฟถูกกว่า VPS) หรือทดลอง VPS เดือนเดียวดูระบบ |
| **≥ $500** | **เปิด Exness Free VPS** (ฟรี + latency ต่ำใกล้เซิร์ฟเวอร์โบรก) = จุดคุ้มสุด |
| ≥ $1,000 | VPS เสียเงินก็เริ่มคุ้ม (ค่า VPS <2% ของพอร์ต) |

> 💡 Exness แจก VPS ฟรีเมื่อฝาก ≥ $500 และเทรดต่อเนื่อง — นี่คือทางที่ดีที่สุด ไม่ต้องจ่าย + เร็ว

---

## 1) เลือก VPS
- **สเปกขั้นต่ำ:** 2 vCPU · 4GB RAM · 50GB SSD · **Windows Server 2022**
- **Region:** ใกล้เซิร์ฟเวอร์โบรก (เช็ก ping ใน MT5 มุมขวาล่าง) — แต่กลยุทธ์เราเป็น M15 ไม่ไวระดับ HFT จึงไม่ซีเรียสมาก
- **ตัวเลือก:** Exness Free VPS (≥$500) · Cloudzy/Kamatera/Vultr/AWS Lightsail Windows (~$8–16/mo) · ForexVPS/FXVM (low-latency ~$30/mo)

## 2) ตั้งค่า VPS ครั้งแรก (หลัง RDP เข้า)
1. Windows Update ให้ครบ
2. **ความปลอดภัย (สำคัญ — config มีรหัส MT5):**
   - ตั้งรหัส RDP **ยาว+ซับซ้อน** (RDP โดน brute-force บ่อยมาก)
   - เปิด Windows Firewall · จำกัด RDP เฉพาะ IP คุณถ้าทำได้ · พิจารณาเปลี่ยนพอร์ต RDP
   - อย่าเปิดพอร์ตอื่นนอกจาก RDP

## 3) ติดตั้งโปรแกรม
1. **MT5 (Exness)** → ติดตั้ง → ล็อกอินบัญชีจริง → เปิด **AutoTrading (Algo Trading)** · Tools > Options > Expert Advisors ติ๊ก allow
2. **Python 3.12** → ดาวน์โหลดจาก python.org → ติดตั้ง **ติ๊ก ✅ "Add Python to PATH"**
3. เปิด PowerShell/CMD แล้วลง dependencies:
   ```
   pip install MetaTrader5 pandas numpy requests
   ```

## 4) ก็อปไฟล์บอทขึ้น VPS
ก็อป **ทั้งโฟลเดอร์โปรเจกต์** `cdc-action-zone-alert\` ขึ้น VPS (เช่นวางที่ `C:\bot\cdc-action-zone-alert\`)
- ต้องมีทั้ง `part2_mt5\` **และ** `core\` (part2 เรียก `core.signals` ของ Part 1)
- วิธีก็อป: ZIP โฟลเดอร์ → ลากผ่าน RDP / Google Drive / GitHub

## 5) ตั้งค่า config.env บน VPS
แก้ `part2_mt5\config.env`:
- `MT5_LOGIN / MT5_PASSWORD / MT5_SERVER` = บัญชีจริง
- `MT5_TERMINAL_PATH` = path เต็มของ terminal64.exe บน VPS (บอทจะเปิด+ล็อกอิน MT5 ให้เอง)
- ค่าอื่น (risk/scalp/flags) ก็อปจากเครื่องเดิมมาได้เลย

## 6) ตั้ง Auto-start (VPS = รันตลอด)
1. ก็อป `vps_run.bat` (ในโฟลเดอร์นี้) — มันใช้ path ของตัวเอง (`%~dp0`) รันได้ทุกที่
2. สร้าง shortcut ของ `vps_run.bat` ใส่ใน **Startup folder**:
   `Win+R` → พิมพ์ `shell:startup` → วาง shortcut ที่นั่น
3. **เปิด Windows Auto-login** (เพื่อให้รีบูตแล้วล็อกอิน→startup→บอทรันเอง):
   `Win+R` → `netplwiz` → เอาติ๊ก "Users must enter a user name and password" ออก → ใส่รหัส
   > ⚠️ Auto-login = ใครเข้าถึงเครื่องได้จะเข้า Windows ได้เลย — ยอมรับได้บน VPS ส่วนตัวที่ RDP ล็อกแน่น

## 7) ทดสอบ
1. ดับเบิลคลิก `vps_run.bat` → ดู MT5 เปิด+ล็อกอิน → Telegram เด้ง "Scalping Bot เริ่มทำงาน"
2. เช็ก `part2.log` ไม่มี error
3. รีสตาร์ท VPS 1 ครั้ง → ดูว่าบอทกลับมาเองอัตโนมัติ (auto-login + startup ทำงาน)

## 8) ปิด Fast Startup บน VPS ด้วย (กัน event เพี้ยน)
ดับเบิลคลิก `disable_fast_startup.bat` (กด Yes) — หรือ VPS Windows Server มักปิดอยู่แล้ว

---

## 🔧 ดูแลต่อเนื่อง
- **อัปเดตบอท:** ก็อปไฟล์ .py ทับ → ปิด `python.exe` (Task Manager) → vps_run.bat รีรันให้เอง
- **มอนิเตอร์:** ดูผ่าน Telegram (รายงานทุกไม้ + /status + /insights) ไม่ต้อง RDP บ่อย
- **หยุดบอท:** ลบไฟล์ `part2_should_run.flag` (แต่ vps_run.bat สร้างใหม่ตอน logon — ถ้าอยากหยุดถาวรให้เอา shortcut ออกจาก Startup)

## 🔐 เช็กลิสต์ความปลอดภัย
- [ ] รหัส RDP ยาว+ซับซ้อน
- [ ] Firewall เปิด · RDP จำกัด IP/เปลี่ยนพอร์ต
- [ ] config.env ไม่ commit ขึ้น git (มีรหัส MT5)
- [ ] Windows Update อัตโนมัติ
- [ ] บัญชี MT5 เป็น Demo ก่อน 1 สัปดาห์บน VPS แล้วค่อยสลับ Real (กันตั้งค่าพลาดบนเครื่องใหม่)
