@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
:: เก็บ log ในโฟลเดอร์ OneDrive เพื่อ sync ขึ้น cloud อัตโนมัติ (uncomment + แก้ path ให้ตรงเครื่อง)
:: set PART2_LOG_DIR=C:\Users\Beast\OneDrive\bot-logs
:: %~dp0 = directory ของ bat file เอง (ใช้ได้ทั้ง local และ VPS ไม่ต้อง hardcode path)
cd /d "%~dp0"
:: สร้าง flag ถ้ายังไม่มี (ถูกลบโดย /stop → ต้องสร้างใหม่เมื่อ start)
if not exist "scalpbot_should_run.flag" echo. > scalpbot_should_run.flag
:loop
if not exist "scalpbot_should_run.flag" goto end
echo [%date% %time%] Starting interactive.py ... >> scalpbot.log
REM Python จัดการเขียน+rotate scalpbot.log เอง (เก็บ 2 วัน) — ห้าม redirect ซ้ำ
python interactive.py
if errorlevel 2 goto end
if not exist "scalpbot_should_run.flag" goto end
echo [%date% %time%] interactive.py exited - restart in 15s >> scalpbot.log
ping -n 16 127.0.0.1 >nul
goto loop
:end
echo [%date% %time%] bot stopped >> scalpbot.log
