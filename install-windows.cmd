@echo off
setlocal
powershell.exe -NoLogo -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
echo.
pause
endlocal
