@echo off
REM ============================================================
REM  vps_run.bat - รันบอท Part 2 บน Windows VPS (24/7 always-on)
REM  วางไฟล์นี้ในโฟลเดอร์ part2_mt5 บน VPS แล้วให้รันตอน logon
REM  ต่างจากเครื่องบ้าน: VPS = สร้าง flag เอง รันตลอด ไม่ต้องกด Start
REM  *** ติดตั้ง Python ตอน setup ให้ติ๊ก "Add Python to PATH" ***
REM ============================================================
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo running > "part2_should_run.flag"
:loop
python interactive.py >> part2.log 2>&1
if errorlevel 2 goto end
if not exist "part2_should_run.flag" goto end
echo [%date% %time%] interactive.py exited - restart in 15s >> part2.log
ping -n 16 127.0.0.1 >nul
goto loop
:end
echo [%date% %time%] bot stopped >> part2.log
