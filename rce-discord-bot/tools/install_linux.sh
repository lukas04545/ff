#!/usr/bin/env bash
# ==================================================================
#  Install the RCE Discord bot on a Linux server (Ubuntu / Debian)
#  as a service: starts with the server, restarts after crashes and
#  after automatic updates.
#
#  Run as root:
#     bash install_linux.sh                      (fresh install)
#     bash install_linux.sh /root/backup-….zip   (move an existing bot:
#                                                 restores a backup ZIP made
#                                                 with "Include the .env")
#  Later:  systemctl status rcebot   |   journalctl -u rcebot -f
# ==================================================================
set -euo pipefail

REPO="${REPO:-https://github.com/lukas04545/ff.git}"
BOT_USER="rcebot"
HOME_DIR="/home/$BOT_USER"
BOT_DIR="$HOME_DIR/ff/rce-discord-bot"
BACKUP_ZIP="${1:-}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root, e.g.:  sudo bash $0 $BACKUP_ZIP"; exit 1
fi
if [ -n "$BACKUP_ZIP" ] && [ ! -f "$BACKUP_ZIP" ]; then
    echo "Backup file not found: $BACKUP_ZIP"; exit 1
fi

echo "==> Installing Python, Git and unzip ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git unzip ca-certificates

echo "==> Creating the user '$BOT_USER' (the bot doesn't run as root) ..."
id "$BOT_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$BOT_USER"

if [ ! -d "$HOME_DIR/ff/.git" ]; then
    echo "==> Downloading the bot ..."
    runuser -u "$BOT_USER" -- git clone -q "$REPO" "$HOME_DIR/ff"
else
    echo "==> Bot already downloaded – updating ..."
    runuser -u "$BOT_USER" -- git -C "$HOME_DIR/ff" pull -q --ff-only || true
fi
mkdir -p "$BOT_DIR/data"

if [ -n "$BACKUP_ZIP" ]; then
    echo "==> Restoring the backup ..."
    systemctl stop rcebot 2>/dev/null || true
    unzip -o -q "$BACKUP_ZIP" -d "$BOT_DIR"
    [ -f "$BOT_DIR/bot.db" ] && mv -f "$BOT_DIR/bot.db" "$BOT_DIR/data/bot.db"
    rm -f "$BOT_DIR/README.txt"
    if [ -f "$BOT_DIR/.env" ]; then
        # Windows-only paths from the old PC don't exist here
        sed -i 's/^BACKUP_COPY_DIR=.*/BACKUP_COPY_DIR=/' "$BOT_DIR/.env"
    else
        echo "   (the backup has no .env – you'll enter the settings in the browser on the first start)"
    fi
fi
chown -R "$BOT_USER:$BOT_USER" "$HOME_DIR/ff"
chmod +x "$BOT_DIR/start.sh"
[ -f "$BOT_DIR/.env" ] && chmod 600 "$BOT_DIR/.env"

echo "==> Setting up the service 'rcebot' ..."
cat > /etc/systemd/system/rcebot.service <<EOF
[Unit]
Description=RCE Discord bot
After=network-online.target
Wants=network-online.target

[Service]
User=$BOT_USER
WorkingDirectory=$BOT_DIR
ExecStart=/bin/bash $BOT_DIR/start.sh
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable rcebot >/dev/null
systemctl restart rcebot

echo
echo "==> Done. The bot is starting (the first start installs its packages, ~1 minute)."
echo "    Live log:            journalctl -u rcebot -f      (Ctrl+C to leave)"
echo "    Web interface link:  cat $BOT_DIR/PUBLIC_LINK.txt  (also posted in your Discord admin log)"
echo "    Stop / start:        systemctl stop rcebot  /  systemctl start rcebot"
echo
echo "    IMPORTANT: keep the bot on your old PC switched OFF – two bots with the same token answer twice."
