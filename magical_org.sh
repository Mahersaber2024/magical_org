#!/usr/bin/env bash
# magical_org installer / manager
# One-line install:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Mahersaber2024/magical_org/main/magical_org.sh)

set -uo pipefail

REPO="Mahersaber2024/magical_org"
BRANCH="main"
DIR="/opt/magical_org"
SERVICE="magical_org"
UNIT="/etc/systemd/system/${SERVICE}.service"
TARBALL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"

G="\033[32m"; R="\033[31m"; Y="\033[33m"; C="\033[36m"; B="\033[1m"; N="\033[0m"
ok()   { echo -e "${G}✔${N} $*"; }
err()  { echo -e "${R}✘${N} $*" >&2; }
info() { echo -e "${C}➜${N} $*"; }

ask() {
  local prompt="$1" var="$2" reply
  read -r -p "$prompt" reply </dev/tty || reply=""
  printf -v "$var" '%s' "$reply"
}

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    err "Run as root:  sudo -i   then run the command again."
    exit 1
  fi
}

is_installed() { [ -f "$DIR/magical_org.py" ]; }

is_running() { systemctl is-active --quiet "$SERVICE" 2>/dev/null; }

install_deps() {
  info "Installing system packages"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -y >/dev/null
    apt-get install -y python3 python3-venv python3-pip curl tar >/dev/null
  else
    command -v python3 >/dev/null 2>&1 || { err "python3 not found. Install Python 3.10+ manually."; exit 1; }
    command -v curl >/dev/null 2>&1 || { err "curl not found."; exit 1; }
  fi
  python3 - <<'PY' || exit 1
import sys
if sys.version_info < (3, 10):
    sys.exit("Python 3.10+ is required, found %d.%d" % sys.version_info[:2])
PY
}

download_code() {
  info "Downloading ${REPO} (${BRANCH})"
  local tmp
  tmp="$(mktemp -d)"
  if ! curl -fsSL "$TARBALL" -o "$tmp/src.tar.gz"; then
    err "Download failed. Is the repository public and is the branch '${BRANCH}' correct?"
    rm -rf "$tmp"
    exit 1
  fi
  mkdir -p "$DIR"
  tar -xzf "$tmp/src.tar.gz" --strip-components=1 -C "$DIR" \
    --exclude='*/.env' --exclude='*/data.json' \
    --exclude='*/session/*.session' --exclude='*/session/*.session-journal' \
    --exclude='*/session/*.encrypted' --exclude='*/session/.*key'
  rm -rf "$tmp"
  ok "Code is in $DIR"
}

setup_venv() {
  info "Setting up Python environment"
  [ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv" || { err "python3-venv is missing."; exit 1; }
  "$DIR/venv/bin/pip" install --upgrade pip >/dev/null
  "$DIR/venv/bin/pip" install -r "$DIR/requirements.txt" || { err "pip install failed."; exit 1; }
  mkdir -p "$DIR/session"
  chmod 700 "$DIR/session"
}

configure_env() {
  local token admins tz
  while :; do
    ask "Bot token (from @BotFather): " token
    [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && break
    err "That does not look like a bot token."
  done
  while :; do
    ask "Admin Telegram user id(s), comma separated: " admins
    admins="${admins// /}"
    [[ "$admins" =~ ^[0-9]+(,[0-9]+)*$ ]] && break
    err "Enter numeric ids like 12345 or 12345,67890."
  done
  ask "Timezone [Asia/Tehran]: " tz
  tz="${tz:-Asia/Tehran}"
  cat > "$DIR/.env" <<ENV
BOT_TOKEN=${token}
ADMIN_IDS=${admins}
TIMEZONE=${tz}
DATA_FILE=data.json
ENV
  chmod 600 "$DIR/.env"
  ok ".env saved"
}

write_service() {
  cat > "$UNIT" <<UNITFILE
[Unit]
Description=magical_org Telegram signal bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${DIR}
ExecStart=${DIR}/venv/bin/python ${DIR}/magical_org.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITFILE
  systemctl daemon-reload
  systemctl enable --now "$SERVICE" >/dev/null 2>&1
  ok "Service '${SERVICE}' enabled and started"
}

do_install() {
  if is_installed; then
    err "Already installed. Use Update, or Uninstall first."
    return
  fi
  install_deps
  download_code
  setup_venv
  [ -f "$DIR/.env" ] || configure_env
  write_service
  echo
  ok "Installed. Open your bot in Telegram and send /start"
}

do_update() {
  is_installed || { err "Not installed yet."; return; }
  download_code
  setup_venv
  systemctl restart "$SERVICE"
  ok "Updated. Your .env, data.json and sessions were kept."
}

do_reconfigure() {
  is_installed || { err "Not installed yet."; return; }
  configure_env
  systemctl restart "$SERVICE"
}

do_uninstall() {
  is_installed || [ -f "$UNIT" ] || { err "Nothing to uninstall."; return; }
  local yes wipe
  ask "Remove the magical_org service? [y/N]: " yes
  [[ "$yes" =~ ^[Yy]$ ]] || { info "Cancelled."; return; }
  systemctl disable --now "$SERVICE" >/dev/null 2>&1
  rm -f "$UNIT"
  systemctl daemon-reload
  ok "Service removed"
  ask "Also delete ALL files in $DIR (code, .env, data.json, Telegram sessions)? [y/N]: " wipe
  if [[ "$wipe" =~ ^[Yy]$ ]]; then
    rm -rf "$DIR"
    ok "Everything deleted"
  else
    rm -rf "$DIR/venv"
    info "Kept $DIR (config, data and sessions). Delete it manually if you no longer need it."
  fi
}

show_status() {
  is_installed || { err "Not installed yet."; return; }
  systemctl status "$SERVICE" --no-pager -l | head -n 15
}

show_logs() {
  is_installed || { err "Not installed yet."; return; }
  journalctl -u "$SERVICE" -n 60 --no-pager
}

menu() {
  while :; do
    local state="not installed"
    if is_installed; then
      is_running && state="${G}running${N}" || state="${Y}stopped${N}"
    fi
    echo
    echo -e "${B}========== magical_org ==========${N}"
    echo -e "Status: ${state}"
    echo "  1) Install"
    echo "  2) Update"
    echo "  3) Restart"
    echo "  4) Status"
    echo "  5) Logs"
    echo "  6) Change token / admins"
    echo "  7) Uninstall"
    echo "  0) Exit"
    local c
    ask "Choose: " c
    case "$c" in
      1) do_install ;;
      2) do_update ;;
      3) is_installed && systemctl restart "$SERVICE" && ok "Restarted" || err "Not installed yet." ;;
      4) show_status ;;
      5) show_logs ;;
      6) do_reconfigure ;;
      7) do_uninstall ;;
      0) exit 0 ;;
      *) err "Invalid choice" ;;
    esac
  done
}

need_root
case "${1:-menu}" in
  install)   do_install ;;
  update)    do_update ;;
  uninstall) do_uninstall ;;
  menu|*)    menu ;;
esac
