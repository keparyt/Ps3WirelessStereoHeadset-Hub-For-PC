Option Explicit

Dim shell, fso, scriptDir, appDir, pythonw, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
appDir = fso.BuildPath(scriptDir, "app")
pythonw = "pythonw.exe"

If Not fso.FolderExists(appDir) Then
    WScript.Quit 1
End If

shell.CurrentDirectory = appDir
command = """" & pythonw & """ """ & fso.BuildPath(appDir, "main.py") & """ --tray"

shell.Run command, 0, False
