@echo off
rem Read-only drive check. Asks for administrator rights, then saves drive-report.txt here.
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    rem The path goes through an environment variable so apostrophes in folder names work.
    set "SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:SELF -Verb RunAs"
    exit /b
)
".venv\Scripts\python.exe" -m apfs_explorer_lite.diagnose --out "%~dp0drive-report.txt"
echo.
echo Done. You can close this window.
pause
