Dim fso, sh, d
d = "D:\Cluade Project\cdc-action-zone-alert\part2_mt5"
Set fso = CreateObject("Scripting.FileSystemObject")
If fso.FileExists(d & "\part2_should_run.flag") Then
  Set sh = CreateObject("WScript.Shell")
  sh.CurrentDirectory = d
  sh.Run """" & d & "\start_loop.bat""", 0, False
End If
