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
"${SSH[@]}" "$TARGET" "bash $DIR/install.sh"

echo
echo ">> done. Open the panel through an SSH tunnel:"
echo "   ${SSH[*]} -L 8000:127.0.0.1:8000 $TARGET"
echo "   then browse http://localhost:8000"
