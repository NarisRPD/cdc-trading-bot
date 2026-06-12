@echo off
REM ============================================================
REM  vps_migrate_rebrand.bat — ตัวช่วยย้ายบ้านครั้งเดียวบน VPS
REM  (part2_mt5 -> scalping_bot · รันหลัง git pull ในโฟลเดอร์ใหม่)
REM  1) แก้ Startup: ลบ CDC-Part2-VPS.lnk เก่า + สร้าง ScalpingBot-VPS.cmd
REM  2) ลบโฟลเดอร์เก่าที่ว่างแล้ว  3) สตาร์ทบอท
REM ============================================================
chcp 65001 >nul
set SU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
del "%SU%\CDC-Part2-VPS.lnk" 2>nul
del "%SU%\ScalpingBot-VPS.cmd" 2>nul
(
echo @echo off
echo start "" /D C:\bot\scalping_bot C:\bot\scalping_bot\vps_run.bat
)>"%SU%\ScalpingBot-VPS.cmd"
rd /q "C:\bot\part2_mt5" 2>nul
echo === Startup folder ===
dir /b "%SU%"
echo === ScalpingBot-VPS.cmd ===
type "%SU%\ScalpingBot-VPS.cmd"
echo === Starting bot ===
start "" /D C:\bot\scalping_bot C:\bot\scalping_bot\vps_run.bat
echo DONE
