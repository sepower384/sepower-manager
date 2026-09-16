Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Environment("Process")("PYTHONUTF8") = "1"
sh.Run "pythonw.exe """ & dir & "\run.py"" serve", 0, False
