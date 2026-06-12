@echo off
chcp 65001 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo ขอสิทธิ์ Admin... กด Yes ในหน้าต่าง UAC
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f
echo.
echo ============================================
echo  Fast Startup ปิดแล้ว (HiberbootEnabled=0)
echo  การปิดเครื่อง/บูตจะถูกบันทึก event ถูกต้อง
echo ============================================
echo.
pause
