' start_hidden.vbs — รัน start_loop.bat แบบซ่อนหน้าต่าง (หาโฟลเดอร์ตัวเองอัตโนมัติ)
Dim fso, d
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & d & "\start_loop.bat""", 0, False
