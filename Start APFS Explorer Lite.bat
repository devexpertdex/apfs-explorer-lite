@echo off
rem Double-click to open APFS Explorer Lite.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo The .venv folder is missing. See README.md, section "Setup".
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m apfs_explorer_lite
