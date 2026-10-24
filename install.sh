#!/usr/bin/env bash
# Install / update on the server itself (Debian/Ubuntu, as root), from inside
# a checkout of this repo:   sudo ./install.sh
# Copies the code to /opt/self-auto-reserve and runs it as a systemd service
# on 127.0.0.1:$PORT (default 8000; PORT=8100 ./install.sh to change). Safe to re-run; .env and data/ are kept.
set -euo pipefail
DIR=/opt/self-auto-reserve
PORT=${PORT:-8000}
SRC=$(cd "$(dirname "$0")" && pwd)
if [ "$SRC" != "$DIR" ]; then
  mkdir -p "$DIR"
  tar -C "$SRC" --exclude=.git --exclude=.venv --exclude=data --exclude=.env -cf - . | tar -x -C "$DIR"
fi
cd $DIR
export DEBIAN_FRONTEND=noninteractive
if ! command -v tesseract >/dev/null || ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q python3 python3-venv python3-pip tesseract-ocr curl
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/playwright install --with-deps chromium

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" .env
  sed -i "s|^INVITE_CODE=.*|INVITE_CODE=$(python3 -c 'import secrets; print(secrets.token_hex(4))')|" .env
fi
mkdir -p data

cat > /etc/systemd/system/self-auto-reserve.service <<UNIT
[Unit]
Description=self-auto-reserve food basket bot
After=network-online.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
Environment=DATA_DIR=$DIR/data
ExecStart=$DIR/.venv/bin/uvicorn app.web:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q self-auto-reserve
systemctl restart self-auto-reserve
sleep 3
systemctl is-active self-auto-reserve || { journalctl -u self-auto-reserve -n 30 --no-pager; exit 1; }
echo "invite code: $(grep ^INVITE_CODE= .env | cut -d= -f2)"
echo -n "university site from server: "
curl -s -o /dev/null -m 15 -w "%{http_code}\n" https://nut.uk.ac.ir/ || echo "UNREACHABLE"
