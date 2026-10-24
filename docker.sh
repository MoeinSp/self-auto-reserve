#!/usr/bin/env bash
# Build and start with Docker (run on the server, inside the repo):
#   ./docker.sh            # port 8437
#   PORT=9000 ./docker.sh
# Creates .env with random secrets on first run; safe to re-run for updates.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  echo "Docker is not installed: https://docs.docker.com/engine/install/" >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(head -c 36 /dev/urandom | base64 | tr '+/' '-_')|" .env
  sed -i "s|^INVITE_CODE=.*|INVITE_CODE=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')|" .env
  echo ">> created .env"
fi
grep -q '^PORT=' .env || echo "PORT=${PORT:-8437}" >> .env
mkdir -p data dumps

docker compose --env-file .env up -d --build
sleep 5
docker compose ps
echo
echo "invite code: $(grep ^INVITE_CODE= .env | cut -d= -f2)"
echo "panel:       http://127.0.0.1:$(grep ^PORT= .env | cut -d= -f2)  (open it through an SSH tunnel)"
echo -n "university site from the container: "
docker compose exec -T app python -c "
import urllib.request
try:
    print(urllib.request.urlopen('https://nut.uk.ac.ir/', timeout=15).status)
except Exception as e:
    print('UNREACHABLE', e)
"
