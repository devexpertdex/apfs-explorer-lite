@echo off
rem Read-only hardware test on the APFS SSD, then the full test report (TEST_REPORT.md).
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    rem The path goes through an environment variable so apostrophes in folder names work.
    set "SELF=%~f0"
    powershell -NoProfile -Command "Start-Process -FilePath $env:SELF -Verb RunAs"
    exit /b
)
echo APFS Explorer Lite - hardware test
echo Nothing is written to the APFS SSD. Test copies go to a temporary folder and are deleted.
echo Close APFS Explorer Lite before continuing.
echo.
choice /c YN /n /m "Also test unplugging the SSD during a copy at the end? [Y/N] "
if errorlevel 2 (set UNPLUG=) else (set UNPLUG=--unplug-test)
echo.
".venv\Scripts\python.exe" -m apfs_explorer_lite.hwtest %UNPLUG% --out "%~dp0hardware-results.json"
echo.
echo Building the test report (about a minute)...
".venv\Scripts\python.exe" scripts\make_test_report.py --live --hardware "%~dp0hardware-results.json" > "%~dp0test-run.log" 2>&1
echo Report saved: %~dp0TEST_REPORT.md
echo.
pause
