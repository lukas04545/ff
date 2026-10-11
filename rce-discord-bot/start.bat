@echo off
REM ==================================================================
REM  Start the RCE Discord bot (Windows)
REM  Just double-click: sets everything up on the first start and
REM  restarts the bot automatically after crashes.
REM ==================================================================
chcp 65001 >nul
cd /d "%~dp0"
title RCE Discord Bot

REM --- Find Python (py launcher preferred) ---
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo [ERROR] Python was not found.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

REM --- Set up the virtual environment (first start only) ---
if not exist ".venv\Scripts\python.exe" (
    echo Setting up Python environment ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Could not create .venv.
        pause
        exit /b 1
    )
)

echo Checking/installing dependencies ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Installing dependencies failed. Check your internet connection.
    pause
    exit /b 1
)

REM --- Create .env if it doesn't exist yet (then fill it in in the browser) ---
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo First start: you'll enter the settings in your browser in a moment.
    echo If no browser opens: copy the link from this window.
)

REM --- Start the bot, restart it after a crash ---
:loop
echo.
echo Starting bot ... (keep this window open, stop with Ctrl+C)
".venv\Scripts\python.exe" bot.py
set "CODE=%errorlevel%"
if "%CODE%"=="3" (
    echo.
    echo [ERROR] The bot stopped because of a configuration error - see the message above.
    echo Please fix the .env and run start.bat again.
    pause
    exit /b 3
)
if "%CODE%"=="0" goto :end
echo.
echo Bot stopped unexpectedly (code %CODE%). Restarting in 10 seconds ... (Ctrl+C to cancel)
timeout /t 10 /nobreak >nul
goto :loop

:end
echo Bot stopped.
pause
