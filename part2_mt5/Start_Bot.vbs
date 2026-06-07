Dim fso, sh, d
d = "D:\Cluade Project\cdc-action-zone-alert\part2_mt5"
Set fso = CreateObject("Scripting.FileSystemObject")
fso.CreateTextFile(d & "\part2_should_run.flag", True).Close
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = d
sh.Run """" & d & "\start_loop.bat""", 0, False
