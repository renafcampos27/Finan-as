Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(folder, "financas_liquid_glass_launcher.pyw")
stateRoot = fso.BuildPath(shell.ExpandEnvironmentStrings("%LOCALAPPDATA%"), "FinancasPessoais")
logdir = fso.BuildPath(stateRoot, "logs")
If Not fso.FolderExists(stateRoot) Then fso.CreateFolder(stateRoot)
If Not fso.FolderExists(logdir) Then fso.CreateFolder(logdir)
cmd = "cmd.exe /c ""cd /d """ & folder & """ && set QT_OPENGL=software && set QT_QUICK_BACKEND=software && (where pyw.exe >nul 2>nul && pyw.exe -3.14 """ & script & """ || (where pythonw.exe >nul 2>nul && pythonw.exe """ & script & """ || python """ & script & """)) > """ & fso.BuildPath(logdir, "arranque_liquid_glass.log") & """ 2>&1"""
shell.Run cmd, 0, False
