@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
:: %~dp0 = directory ของ bat file เอง (ใช้ได้ทั้ง local และ VPS ไม่ต้อง hardcode path)
cd /d "%~dp0"
:loop
if not exist "part2_should_run.flag" goto end
echo [%date% %time%] Starting interactive.py ... >> part2.log
python interactive.py >> part2.log 2>&1
if errorlevel 2 goto end
if not exist "part2_should_run.flag" goto end
echo [%date% %time%] interactive.py exited - restart in 15s >> part2.log
ping -n 16 127.0.0.1 >nul
goto loop
:end
echo [%date% %time%] bot stopped >> part2.log
