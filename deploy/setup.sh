#!/usr/bin/env bash
# Run on a fresh Ubuntu 24.04 droplet, as root (or via sudo), from the repo
# root: sudo bash deploy/setup.sh
#
# Handles everything that CAN be scripted: system packages, a dedicated
# non-root user, uv, dependencies, the systemd service, the crontab, and the
# firewall. It does NOT transfer .env/secrets/certs — those are gitignored on
# purpose and have to come from your local machine via scp, not this script
# or git. See deploy/README.md for the full sequence including that step.
set -euo pipefail

APP_USER="kalshi"
APP_DIR="/opt/kalshi-prediction-market"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo bash deploy/setup.sh)." >&2
  exit 1
fi

echo "==> Updating system packages"
apt-get update -qq
apt-get install -y -qq git curl ufw

echo "==> Creating dedicated app user ($APP_USER), if not already present"
id -u "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Placing the app at $APP_DIR"
mkdir -p "$APP_DIR"
if [ "$(pwd)" != "$APP_DIR" ]; then
  cp -r . "$APP_DIR"
fi
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> Installing uv for $APP_USER"
sudo -u "$APP_USER" -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

echo "==> Installing Python dependencies"
cd "$APP_DIR"
sudo -u "$APP_USER" -H bash -c "cd '$APP_DIR' && \$HOME/.local/bin/uv sync"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "WARNING: $APP_DIR/.env not found. Copy it from your local machine before" >&2
  echo "starting the dashboard service or installing the crontab — see deploy/README.md." >&2
fi

echo "==> Installing systemd service"
cp deploy/kalshi-dashboard.service /etc/systemd/system/kalshi-dashboard.service
systemctl daemon-reload
systemctl enable kalshi-dashboard

echo "==> Installing crontab for $APP_USER"
sed "s|/path/to/kalshi-prediction-market|$APP_DIR|g" scheduler/crontab.example > /tmp/kalshi-crontab
sudo -u "$APP_USER" crontab /tmp/kalshi-crontab
rm /tmp/kalshi-crontab
mkdir -p "$APP_DIR/logs"
chown "$APP_USER":"$APP_USER" "$APP_DIR/logs"

echo "==> Configuring firewall (SSH only — dashboard is 127.0.0.1-bound, reach it via SSH tunnel)"
ufw allow OpenSSH
ufw --force enable

echo ""
echo "Done. Remaining manual steps:"
echo "  1. If .env wasn't already in place, scp it (+ secrets/, certs/) from your"
echo "     local machine to $APP_DIR, then chown -R $APP_USER:$APP_USER $APP_DIR"
echo "  2. systemctl start kalshi-dashboard"
echo "  3. systemctl status kalshi-dashboard   # confirm it's running"
echo "  4. ssh -L 8000:localhost:8000 <this-droplet>   # then open http://localhost:8000 locally"
