#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"
DIR="$(pwd)"

echo "==> Checking Python"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.10+ first (e.g. sudo apt install python3 python3-venv python3-pip)."
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit("Python 3.10+ is required, found %d.%d" % sys.version_info[:2])
PY

echo "==> Creating virtualenv"
python3 -m venv venv || { echo "Install python3-venv (sudo apt install python3-venv) and rerun."; exit 1; }
# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

mkdir -p session
chmod 700 session

if [ ! -f .env ]; then
  echo "==> Configuring .env"
  read -r -p "Bot token (from @BotFather): " BOT_TOKEN
  read -r -p "Admin Telegram user id(s), comma separated: " ADMIN_IDS
  read -r -p "Timezone [Asia/Tehran]: " TZ_NAME
  TZ_NAME="${TZ_NAME:-Asia/Tehran}"
  cat > .env <<ENV
BOT_TOKEN=${BOT_TOKEN}
ADMIN_IDS=${ADMIN_IDS}
TIMEZONE=${TZ_NAME}
DATA_FILE=data.json
ENV
  chmod 600 .env
else
  echo "==> .env already exists, keeping it"
fi

read -r -p "Install as a systemd service (auto-start on boot)? [y/N]: " SVC
if [[ "$SVC" =~ ^[Yy]$ ]]; then
  SUDO=""
  [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO tee /etc/systemd/system/magical_org.service >/dev/null <<UNIT
[Unit]
Description=magical_org Telegram signal bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=${DIR}
ExecStart=${DIR}/venv/bin/python ${DIR}/magical_org.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now magical_org
  echo "==> Service started. Logs: journalctl -u magical_org -f"
else
  echo "==> Done. Run the bot with:  source venv/bin/activate && python magical_org.py"
fi
