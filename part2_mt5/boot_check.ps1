# boot_check.ps1 - decide at Windows boot whether to resume the Part 2 bot
#   flag present + last shutdown UNEXPECTED (power loss / crash) -> resume (recovery)
#   flag present + last shutdown CLEAN (normal shutdown)         -> remove stale flag, do NOT resume
#   flag absent (user pressed Stop)                              -> do nothing
# Reliable: uses Windows System event 6008 ("previous shutdown was unexpected").
# ASCII-only on purpose (runs under Windows PowerShell 5.1 at boot).
param([switch]$DryRun)
$ErrorActionPreference = "SilentlyContinue"
$dir = "D:\Cluade Project\cdc-action-zone-alert\part2_mt5"
$flag = Join-Path $dir "part2_should_run.flag"
$log = Join-Path $dir "part2.log"

if (-not (Test-Path $flag)) {
    if ($DryRun) { "DRYRUN: no flag -> do nothing (user stopped the bot)" }
    return
}

# Was the LAST shutdown dirty? Compare newest CLEAN event (1074 shutdown/restart initiated,
# 6006 log stop) vs newest DIRTY event (6008 unexpected, 41 kernel-power dirty reboot).
# Robust to Fast Startup: a normal shutdown ALWAYS logs 1074, so clean wins when newer.
$cleanEv = Get-WinEvent -FilterHashtable @{LogName = 'System'; Id = 1074, 6006 } -MaxEvents 1 -ErrorAction SilentlyContinue
$dirtyEv = Get-WinEvent -FilterHashtable @{LogName = 'System'; Id = 6008, 41 } -MaxEvents 1 -ErrorAction SilentlyContinue
$cleanT = if ($cleanEv) { $cleanEv.TimeCreated } else { [datetime]::MinValue }
$dirtyT = if ($dirtyEv) { $dirtyEv.TimeCreated } else { [datetime]::MinValue }
$dirty = $dirtyT -gt $cleanT

if ($DryRun) {
    "DRYRUN: flag=yes  lastClean=$cleanT  lastDirty=$dirtyT  dirty(power-loss/crash)=$dirty"
    if ($dirty) { "  -> would RESUME bot (recovery)" } else { "  -> would REMOVE stale flag, NOT resume" }
    return
}

if ($dirty) {
    Add-Content $log "[$(Get-Date)] boot_check: unexpected shutdown -> resume bot"
    Start-Process -FilePath (Join-Path $dir "start_loop.bat") -WorkingDirectory $dir -WindowStyle Hidden
}
else {
    Remove-Item $flag -Force
    Add-Content $log "[$(Get-Date)] boot_check: clean shutdown -> removed stale flag, no resume"
}
