Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
WScript.Sleep 5000
sh.Run "cmd /c python -u server.py >> clipdesk_log.txt 2>&1", 0, False
