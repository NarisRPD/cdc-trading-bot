Dim fso, sh, d
d = "D:\Cluade Project\cdc-action-zone-alert\part2_mt5"
Set fso = CreateObject("Scripting.FileSystemObject")
If fso.FileExists(d & "\part2_should_run.flag") Then fso.DeleteFile(d & "\part2_should_run.flag")
Set sh = CreateObject("WScript.Shell")
sh.Popup "Stopping MT5 bot (within ~15s)...", 3, "Part 2 - MT5 Bot", 64
