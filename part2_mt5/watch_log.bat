@echo off
chcp 65001 >nul
title Part 2 — Live Log Monitor
cd /d "%~dp0"
echo ============================================================
echo  Part 2 MT5 Bot — Live Log (Ctrl+C เพื่อหยุด)
echo  Path: %~dp0part2.log
echo ============================================================
echo.
powershell -NoProfile -Command "Get-Content -Path '%~dp0part2.log' -Wait -Tail 80"
