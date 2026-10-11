@echo off
REM RCON-Verbindungstest: prueft Schritt fuer Schritt, warum RCON (nicht) klappt.
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Bitte zuerst einmal start.bat ausfuehren, damit alles installiert wird.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" tools\rcon_test.py
echo.
pause
