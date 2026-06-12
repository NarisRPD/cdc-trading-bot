' ScalpingBot-boot.vbs — รันตอนเครื่องบูต: ถ้า flag ยังอยู่ (ผู้ใช้ไม่ได้สั่งหยุด) ให้เริ่มบอทต่อ
Dim fso, sh, d
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
If fso.FileExists(d & "\scalpbot_should_run.flag") Then
  Set sh = CreateObject("WScript.Shell")
  sh.CurrentDirectory = d
  sh.Run """" & d & "\start_loop.bat""", 0, False
End If
