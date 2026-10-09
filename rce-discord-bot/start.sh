#!/usr/bin/env bash
# ==================================================================
#  RCE Discord-Bot starten (Linux / macOS)
#  Richtet beim ersten Start alles ein und startet den Bot nach
#  Abstürzen automatisch neu.
# ==================================================================
set -u
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
    echo "[FEHLER] Python 3.10+ wurde nicht gefunden (z. B. 'sudo apt install python3 python3-venv')."
    exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "Richte Python-Umgebung ein ..."
    "$PY" -m venv .venv || { echo "[FEHLER] venv fehlgeschlagen (Paket python3-venv installiert?)"; exit 1; }
fi

echo "Prüfe/Installiere Abhängigkeiten ..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt || {
    echo "[FEHLER] Installation fehlgeschlagen. Internetverbindung prüfen."; exit 1; }

if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "Neue .env angelegt – bitte ausfüllen (z. B. 'nano .env') und dann erneut starten."
    exit 0
fi

while true; do
    echo "Starte Bot ... (Beenden mit Strg+C)"
    .venv/bin/python bot.py
    code=$?
    if [ "$code" -eq 3 ]; then
        echo "[FEHLER] Konfigurationsfehler – .env korrigieren und erneut starten."
        exit 3
    fi
    if [ "$code" -eq 0 ] || [ "$code" -eq 130 ]; then
        echo "Bot beendet."
        exit 0
    fi
    echo "Bot wurde unerwartet beendet (Code $code). Neustart in 10 Sekunden ..."
    sleep 10
done
