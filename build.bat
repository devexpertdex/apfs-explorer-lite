@echo off
rem Builds the portable app, the .zip and the installer into the "dist" folder.
cd /d "%~dp0"
".venv\Scripts\python.exe" scripts\build_release.py %*
echo.
pause
