#!/usr/bin/env bash
# CryptoBOT — розгортання/оновлення на Linux-сервері з Docker.
#
# Перший раз:
#   git clone https://github.com/tarasgirnyk/cryptobot.git /opt/cryptobot
#   cd /opt/cryptobot
#   cp .env.example .env && nano .env      # виставити реальні значення
#   ./deploy.sh
#
# Оновлення:
#   cd /opt/cryptobot && ./deploy.sh
#
# Панель слухає лише 127.0.0.1 — назовні виводь через SSH-тунель або
# reverse proxy з авторизацією (+ API_BEARER_TOKEN у .env).

set -euo pipefail
cd "$(dirname "$0")"

BRANCH="${CRYPTOBOT_BRANCH:-main}"

echo "==> git fetch && reset --hard origin/${BRANCH}"
git fetch --prune origin
git checkout "${BRANCH}"
git reset --hard "origin/${BRANCH}"

if [[ ! -f .env ]]; then
  echo "!! .env не знайдено. Створи його з .env.example і запусти знову." >&2
  exit 1
fi

# Порт панелі, який публікує compose (той самий дефолт, що в compose.yaml).
PORT="$(sed -n 's/^CRYPTBOT_PUBLIC_PORT=\([0-9]\+\).*/\1/p' .env | head -1)"
PORT="${PORT:-8765}"

PROFILES=()
if [[ -f Caddyfile ]]; then
  PROFILES=(--profile proxy)
  echo "==> Caddyfile present — enabling public HTTPS proxy"
fi

echo "==> docker compose up -d --build"
docker compose "${PROFILES[@]}" up -d --build

echo "==> docker compose ps"
docker compose ps

echo "==> health check (до 30 с)"
for i in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${PORT}/api/health"
    echo
    echo "OK: CryptoBOT відповідає на 127.0.0.1:${PORT}"
    exit 0
  fi
  sleep 2
done

echo "!! health не піднявся — дивись логи:" >&2
echo "   docker compose logs -f cryptobot" >&2
exit 1
