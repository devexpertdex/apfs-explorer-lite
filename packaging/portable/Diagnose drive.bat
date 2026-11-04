@echo off
rem Read-only drive diagnostic. Asks for administrator rights, then saves a report in Documents.
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    rem The path goes through an environment variable so apostrophes in folder names work.
    set "SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:SELF -Verb RunAs"
    exit /b
)
"%~dp0APFS Explorer Lite Tools.exe" diagnose --out "%USERPROFILE%\Documents\APFS Explorer Lite drive report.txt"
echo.
echo Done. You can close this window.
pause
