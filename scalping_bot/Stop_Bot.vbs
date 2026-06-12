' Stop_Bot.vbs — หยุด Scalping Bot Trade by narisrpd (ลบ flag → loop จบเอง)
Dim fso, sh, d
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
If fso.FileExists(d & "\scalpbot_should_run.flag") Then fso.DeleteFile(d & "\scalpbot_should_run.flag")
Set sh = CreateObject("WScript.Shell")
sh.Popup "Stopping Scalping Bot (within ~15s)...", 3, "Scalping Bot Trade by narisrpd", 64
