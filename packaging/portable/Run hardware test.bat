@echo off
rem Read-only hardware test of this packaged build on the external APFS SSD.
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    rem The path goes through an environment variable so apostrophes in folder names work.
    set "SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:SELF -Verb RunAs"
    exit /b
)
echo APFS Explorer Lite - hardware test (read-only)
echo Nothing is written to the APFS SSD. Test copies go to a temporary folder and are deleted.
echo Close APFS Explorer Lite before continuing.
echo.
choice /c YN /n /m "Also test unplugging the SSD during a copy at the end? [Y/N] "
if errorlevel 2 (set UNPLUG=) else (set UNPLUG=--unplug-test)
echo.
"%~dp0APFS Explorer Lite Tools.exe" hwtest %UNPLUG% --out "%USERPROFILE%\Documents\APFS Explorer Lite hardware test.json"
echo.
pause
