Option Explicit

Dim arguments, shell, fileSystem, powerShellExecutable, scriptPath, command, index

Set arguments = WScript.Arguments
If arguments.Count < 1 Then
    WScript.Quit 2
End If

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

powerShellExecutable = fileSystem.BuildPath( _
    shell.ExpandEnvironmentStrings("%SystemRoot%"), _
    "System32\WindowsPowerShell\v1.0\powershell.exe" _
)
scriptPath = arguments(0)

command = QuoteArgument(powerShellExecutable) & _
    " -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & _
    QuoteArgument(scriptPath)

For index = 1 To arguments.Count - 1
    command = command & " " & QuoteArgument(arguments(index))
Next

' WScript is a GUI-subsystem host. Window style 0 keeps the whole PowerShell
' process tree off the interactive desktop while True preserves task lifetime.
WScript.Quit shell.Run(command, 0, True)

Function QuoteArgument(value)
    QuoteArgument = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function
