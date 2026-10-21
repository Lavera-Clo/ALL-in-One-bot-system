#!/usr/bin/env bash
# One-time setup of the AI bot server on a fresh Ubuntu 22.04 / 24.04 machine
# (Oracle Cloud Always Free, Google Cloud e2-micro, or any small VPS).
#
#   curl -fsSL https://raw.githubusercontent.com/Lavera-Clo/ALL-in-One-bot-system/main/deploy/setup_server.sh -o setup.sh
#   sudo bash setup.sh bot.yourdomain.com
#
# It installs Python and Caddy (automatic HTTPS), puts the code in /opt/whatsapp-bot, creates
# the 'whatsapp-bot' service that starts on boot and restarts if it stops, and serves it at
# https://<your domain>. Run it again any time to update to the latest code.
set -euo pipefail

DOMAIN="${1:-}"
REPO="${REPO:-https://github.com/Lavera-Clo/ALL-in-One-bot-system.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/whatsapp-bot
APP_USER=whatsappbot

if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo bash setup.sh bot.yourdomain.com"; exit 1; fi
if [[ -z "$DOMAIN" ]]; then echo "Give your domain: sudo bash setup.sh bot.yourdomain.com"; exit 1; fi

echo "==> Packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip python3-dev build-essential git curl debian-keyring debian-archive-keyring apt-transport-https
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y && apt-get install -y caddy
fi

echo "==> App user and code"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
# Where the code comes from: this script's own project folder (copied to the server, no GitHub
# needed), or GitHub when the script is run on its own.
SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -f "$SOURCE_DIR/requirements.txt" && -f "$SOURCE_DIR/main.py" ]]; then
  echo "Installing from the project copy in $SOURCE_DIR"
  mkdir -p "$APP_DIR"
  cp -r "$SOURCE_DIR/." "$APP_DIR/"
  rm -rf "$APP_DIR/.git" "$APP_DIR/.venv" "$APP_DIR/__pycache__"
  chown -R "$APP_USER" "$APP_DIR"
elif [[ -d "$APP_DIR/.git" ]]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
  mkdir -p "$APP_DIR" && chown "$APP_USER" "$APP_DIR"
  sudo -u "$APP_USER" git clone --branch "$BRANCH" "$REPO" "$APP_DIR"
fi
mkdir -p "$APP_DIR/uploads" "$APP_DIR/static/tenant_docs" && chown -R "$APP_USER" "$APP_DIR"

echo "==> Python packages"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
# Not --quiet: installing takes a few minutes on a small server, and silence looks like a hang.
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
# Fail here, loudly, rather than later as a service that restarts forever.
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -c "import fastapi, uvicorn, sqlalchemy, greenlet, asyncpg, aiosqlite, openai, av, numpy; print('Python packages OK')"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  sed -i "s|^BASE_URL=.*|BASE_URL=https://$DOMAIN|" "$APP_DIR/.env"
  chown "$APP_USER" "$APP_DIR/.env" && chmod 600 "$APP_DIR/.env"
  NEW_ENV=1
fi

echo "==> Service"
cp "$APP_DIR/deploy/whatsapp-bot.service" /etc/systemd/system/whatsapp-bot.service
systemctl daemon-reload
systemctl enable whatsapp-bot >/dev/null

echo "==> HTTPS (Caddy)"
sed "s|bot.yourdomain.com|$DOMAIN|" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

echo "==> Firewall: allow web traffic"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp; ufw allow 443/tcp; fi
# Oracle Cloud images block ports with iptables rules as well as the cloud firewall.
if iptables -L INPUT -n 2>/dev/null | grep -q "REJECT"; then
  iptables -I INPUT -p tcp --dport 80 -j ACCEPT
  iptables -I INPUT -p tcp --dport 443 -j ACCEPT
  command -v netfilter-persistent >/dev/null && netfilter-persistent save || true
fi

if [[ "${NEW_ENV:-0}" == "1" ]]; then
  echo
  echo "Almost done. Fill in your settings, then start the bot:"
  echo "  sudo nano $APP_DIR/.env        (DATABASE_URL, META_APP_SECRET, META_VERIFY_TOKEN, ADMIN_PASSWORD,"
  echo "                                  AI keys, CORS_ORIGINS=https://your-website.vercel.app)"
  echo "  sudo systemctl start whatsapp-bot"
else
  systemctl restart whatsapp-bot
fi
echo
echo "Check it:   curl https://$DOMAIN/health     Logs:   sudo journalctl -u whatsapp-bot -f"
