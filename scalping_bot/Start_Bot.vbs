' Start_Bot.vbs — เริ่ม Scalping Bot Trade by narisrpd (หาโฟลเดอร์ตัวเองอัตโนมัติ)
Dim fso, sh, d
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
fso.CreateTextFile(d & "\scalpbot_should_run.flag", True).Close
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = d
sh.Run """" & d & "\start_loop.bat""", 0, False
