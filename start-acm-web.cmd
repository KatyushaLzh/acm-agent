@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0acm.ps1" web
if errorlevel 1 (
  echo.
  echo ACM Agent failed to start. Press any key to close.
  pause >nul
)
endlocal
