#!/usr/bin/env bash
# One-time setup on Ubuntu 22.04 / 24.04 (a VPS, or WSL2 with systemd enabled).
# Usage, from the repo folder:   sudo bash deploy/setup.sh
# Safe to run again: it updates the code and keeps your existing .env.
set -euo pipefail

APP_DIR=/opt/ai-bots
BOT_USER=botuser
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo: sudo bash deploy/setup.sh" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip

echo "==> Creating service user '$BOT_USER'"
if ! id -u "$BOT_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$BOT_USER"
fi
# Lets the watchdog read other services' logs with journalctl
usermod -aG systemd-journal "$BOT_USER"

echo "==> Copying code to $APP_DIR"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/bots"
cp -r "$SRC_DIR/bots" "$SRC_DIR/requirements.txt" "$APP_DIR/"
if [ ! -f "$APP_DIR/.env" ]; then
  if [ -f "$SRC_DIR/.env" ]; then cp "$SRC_DIR/.env" "$APP_DIR/.env"; else cp "$SRC_DIR/.env.example" "$APP_DIR/.env"; fi
  echo "    Created $APP_DIR/.env - edit it before starting the bots."
fi

echo "==> Creating virtualenv and installing dependencies"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

chown -R "$BOT_USER:$BOT_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Installing systemd units and sudoers rule"
install -m 644 "$SRC_DIR/deploy/ai-bot.service" /etc/systemd/system/ai-bot.service
install -m 644 "$SRC_DIR/deploy/watchdog-bot.service" /etc/systemd/system/watchdog-bot.service
install -m 440 "$SRC_DIR/deploy/sudoers-ai-bots" /etc/sudoers.d/ai-bots
visudo -cf /etc/sudoers.d/ai-bots
systemctl daemon-reload

cat <<EOF

Done. Next steps:
  1. sudo nano $APP_DIR/.env          # fill in tokens, AI key, ADMIN_CHAT_ID
  2. sudo systemctl enable --now ai-bot watchdog-bot
  3. systemctl status ai-bot watchdog-bot
  4. journalctl -u ai-bot -f          # follow the logs (Ctrl+C to stop)

After changing code or .env later:  sudo bash deploy/setup.sh && sudo systemctl restart ai-bot watchdog-bot
EOF
