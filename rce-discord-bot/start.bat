@echo off
REM ==================================================================
REM  RCE Discord-Bot starten (Windows)
REM  Doppelklick genuegt: richtet beim ersten Start alles ein und
REM  startet den Bot nach Abstuerzen automatisch neu.
REM ==================================================================
chcp 65001 >nul
cd /d "%~dp0"
title RCE Discord-Bot

REM --- Python finden (py-Launcher bevorzugt) ---
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo [FEHLER] Python wurde nicht gefunden.
    echo Installiere Python 3.10 oder neuer von https://www.python.org/downloads/
    echo und setze bei der Installation den Haken bei "Add Python to PATH".
    pause
    exit /b 1
)

REM --- Virtuelle Umgebung einrichten (nur beim ersten Start) ---
if not exist ".venv\Scripts\python.exe" (
    echo Richte Python-Umgebung ein ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [FEHLER] Konnte .venv nicht anlegen.
        pause
        exit /b 1
    )
)

echo Pruefe/Installiere Abhaengigkeiten ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo [FEHLER] Installation der Abhaengigkeiten fehlgeschlagen. Internetverbindung pruefen.
    pause
    exit /b 1
)

REM --- .env anlegen, falls noch nicht vorhanden ---
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Es wurde eine neue .env-Datei angelegt.
    echo Trage dort Discord-Token, RCON-Daten usw. ein, speichere und schliesse den Editor.
    notepad ".env"
)

REM --- Bot starten, bei Absturz neu starten ---
:loop
echo.
echo Starte Bot ... (Fenster offen lassen, Beenden mit Strg+C)
".venv\Scripts\python.exe" bot.py
set "CODE=%errorlevel%"
if "%CODE%"=="3" (
    echo.
    echo [FEHLER] Der Bot wurde wegen eines Konfigurationsfehlers beendet - siehe Meldung oben.
    echo Bitte .env korrigieren und start.bat erneut starten.
    pause
    exit /b 3
)
if "%CODE%"=="0" goto :end
echo.
echo Bot wurde unerwartet beendet (Code %CODE%). Neustart in 10 Sekunden ... (Strg+C zum Abbrechen)
timeout /t 10 /nobreak >nul
goto :loop

:end
echo Bot beendet.
pause
