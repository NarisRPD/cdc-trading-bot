@echo off
chcp 65001 >nul
title Part 2 — Live Log Monitor
cd /d "%~dp0"
echo ============================================================
echo  Part 2 MT5 Bot — Live Log (Ctrl+C เพื่อหยุด)
echo  Path: %~dp0part2.log
echo ============================================================
echo.
rem -Encoding UTF8 จำเป็น: log เป็น UTF-8 ไม่มี BOM — PS 5.1 ไม่ใส่จะอ่านเป็น ANSI = ไทยมั่ว
powershell -NoProfile -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Content -Path '%~dp0part2.log' -Wait -Tail 80 -Encoding UTF8"
