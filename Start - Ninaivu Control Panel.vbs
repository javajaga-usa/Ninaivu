' Ninaivu Control Panel - double-click to open it, with no console window.
' It runs ninaivu_control.pyw with the pythonw in this folder's .venv, which
' start.cmd creates the first time it runs.
Option Explicit
Dim shell, files, folder, python, entry
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")
entry = files.BuildPath(folder, "ninaivu_control.pyw")
If Not files.FileExists(python) Then
    MsgBox "Ninaivu is not set up in this folder yet, so there is no Python environment for the Control Panel." & vbCrLf & vbCrLf & _
           "Double-click start.cmd once to set it up, then open the Control Panel again.", 48, "Ninaivu Control Panel"
    WScript.Quit 1
End If
shell.CurrentDirectory = folder
shell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & entry & Chr(34), 1, False
