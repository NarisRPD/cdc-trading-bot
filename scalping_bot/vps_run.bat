@echo off
REM ============================================================
REM  vps_run.bat - รันบอท Scalping Bot บน Windows VPS (24/7 always-on)
REM  วางไฟล์นี้ในโฟลเดอร์ scalping_bot บน VPS แล้วให้รันตอน logon
REM  ต่างจากเครื่องบ้าน: VPS = สร้าง flag เอง รันตลอด ไม่ต้องกด Start
REM  *** ติดตั้ง Python ตอน setup ให้ติ๊ก "Add Python to PATH" ***
REM ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM เก็บ log ในโฟลเดอร์ OneDrive เพื่อ sync ขึ้น cloud อัตโนมัติ (uncomment + แก้ path ให้ตรง VPS)
REM set PART2_LOG_DIR=C:\Users\Administrator\OneDrive\bot-logs
cd /d "%~dp0"
echo running > "scalpbot_should_run.flag"
:loop
REM Python จัดการเขียน+rotate scalpbot.log เอง (เก็บ 2 วัน) — ห้าม redirect ซ้ำ
python interactive.py
if errorlevel 2 goto end
if not exist "scalpbot_should_run.flag" goto end
echo [%date% %time%] interactive.py exited - restart in 15s >> scalpbot.log
ping -n 16 127.0.0.1 >nul
goto loop
:end
echo [%date% %time%] bot stopped >> scalpbot.log
