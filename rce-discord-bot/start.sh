#!/usr/bin/env bash
# ==================================================================
#  Start the RCE Discord bot (Linux / macOS)
#  Sets everything up on the first start and restarts the bot
#  automatically after crashes.
# ==================================================================
set -u
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "[ERROR] Python 3.10+ was not found (e.g. 'sudo apt install python3 python3-venv')."
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Setting up Python environment ..."
    "$PY" -m venv .venv || { echo "[ERROR] venv failed (is the python3-venv package installed?)"; exit 1; }
fi

echo "Checking/installing dependencies ..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || {
    echo "[ERROR] Installation failed. Check your internet connection."; exit 1; }

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "First start: you'll enter the settings in your browser in a moment (link appears below)."
fi

while true; do
    echo "Starting bot ... (stop with Ctrl+C)"
    .venv/bin/python bot.py
    code=$?
    if [ "$code" -eq 3 ]; then
        echo "[ERROR] Configuration error – fix the .env and start again."
        exit 3
    fi
    if [ "$code" -eq 0 ] || [ "$code" -eq 130 ]; then
        echo "Bot stopped."
        exit 0
    fi
    echo "Bot stopped unexpectedly (code $code). Restarting in 10 seconds ..."
    sleep 10
done
