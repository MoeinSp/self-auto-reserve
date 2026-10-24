#!/usr/bin/env bash
# Deploy to a Debian/Ubuntu server over SSH (run from your own machine, inside the repo):
#
#   ./deploy.sh root@SERVER_IP ~/.ssh/YOUR_KEY
#
# Installs into /opt/self-auto-reserve as a systemd service listening on
# 127.0.0.1:8000. Safe to re-run for updates; .env and data/ are kept.
set -euo pipefail

TARGET=${1:?usage: ./deploy.sh user@host [ssh_key]}
KEY=${2:-}
SSH=(ssh -o StrictHostKeyChecking=accept-new)
[ -n "$KEY" ] && SSH+=(-i "$KEY")
DIR=/opt/self-auto-reserve

echo ">> uploading code"
git archive --format=tar HEAD | "${SSH[@]}" "$TARGET" "mkdir -p $DIR && tar -x -C $DIR"

echo ">> installing on server"
"${SSH[@]}" "$TARGET" "bash -s" <<EOF
set -euo pipefail
cd $DIR
export DEBIAN_FRONTEND=noninteractive
if ! command -v tesseract >/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q python3 python3-venv python3-pip tesseract-ocr curl
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/playwright install --with-deps chromium

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" .env
  sed -i "s|^INVITE_CODE=.*|INVITE_CODE=\$(python3 -c 'import secrets; print(secrets.token_hex(4))')|" .env
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
ExecStart=$DIR/.venv/bin/uvicorn app.web:app --host 127.0.0.1 --port 8000
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
echo "invite code: \$(grep ^INVITE_CODE= .env | cut -d= -f2)"
echo -n "university site from server: "
curl -s -o /dev/null -m 15 -w "%{http_code}\n" https://nut.uk.ac.ir/ || echo "UNREACHABLE"
EOF

echo
echo ">> done. Open the panel through an SSH tunnel:"
echo "   ${SSH[*]} -L 8000:127.0.0.1:8000 $TARGET"
echo "   then browse http://localhost:8000"
