@echo off
REM RCON connection test: checks step by step why RCON does (not) work.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run start.bat once first so everything gets installed.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" tools\rcon_test.py
echo.
pause
