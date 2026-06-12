# vps_install.ps1 - one-shot installer for CDC Part 2 bot on a Windows VPS
# Run this INSIDE the extracted scalping_bot folder:  Right-click > Run with PowerShell
# (ASCII-only on purpose: runs fine on Windows Server 2016 / PowerShell 5.1)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
Write-Host "==== CDC Part 2 - VPS installer ====" -ForegroundColor Cyan
Write-Host "Folder: $here"

# 1) Python present?
$pv = $null
try { $pv = (& python --version 2>&1) } catch {}
if (-not $pv) {
    Write-Host "[X] Python not found on PATH." -ForegroundColor Red
    Write-Host "    Install Python 3.12 from python.org and TICK 'Add Python to PATH', then re-run." -ForegroundColor Yellow
    Read-Host "Press Enter to exit"; exit 1
}
Write-Host "[OK] $pv"

# 2) Dependencies
Write-Host "==== Installing dependencies (this can take a minute) ====" -ForegroundColor Cyan
& python -m pip install --upgrade pip
& python -m pip install MetaTrader5 pandas numpy requests
if ($LASTEXITCODE -ne 0) {
    Write-Host "[X] pip install failed - check internet / pip." -ForegroundColor Red
    Read-Host "Press Enter to exit"; exit 1
}
Write-Host "[OK] dependencies installed"

# 3) Quick import smoke test
$test = "import MetaTrader5, pandas, numpy, requests; print('imports OK')"
& python -c $test
if ($LASTEXITCODE -ne 0) { Write-Host "[X] import test failed" -ForegroundColor Red } else { Write-Host "[OK] import test passed" }

# 4) Startup shortcut for vps_run.bat (auto-start on logon)
$bat = Join-Path $here "vps_run.bat"
if (Test-Path $bat) {
    $startup = [Environment]::GetFolderPath("Startup")
    $lnk = Join-Path $startup "CDC-Part2-VPS.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $bat
    $sc.WorkingDirectory = $here
    $sc.WindowStyle = 7
    $sc.Save()
    Write-Host "[OK] Startup shortcut created: $lnk"
} else {
    Write-Host "[!] vps_run.bat not found in this folder" -ForegroundColor Yellow
}

# 5) config.env check
if (Test-Path (Join-Path $here "config.env")) {
    Write-Host "[OK] config.env found"
} else {
    Write-Host "[!] config.env NOT found." -ForegroundColor Yellow
    Write-Host "    Copy config.example.env -> config.env and fill MT5_LOGIN / MT5_PASSWORD /" -ForegroundColor Yellow
    Write-Host "    MT5_SERVER / MT5_TERMINAL_PATH (path of terminal64.exe on this VPS)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==== Install done. Remaining manual steps: ====" -ForegroundColor Cyan
Write-Host " 1) Install MT5 (Exness), log in to the REAL account, enable AutoTrading"
Write-Host " 2) Make sure config.env is filled (esp. MT5_TERMINAL_PATH for this VPS)"
Write-Host " 3) Enable Windows auto-login:  Win+R > netplwiz  (so reboots recover unattended)"
Write-Host " 4) Double-click vps_run.bat to start  (or reboot and let Startup launch it)"
Write-Host " 5) Watch Telegram for the startup message + first scan"
Read-Host "Press Enter to exit"
