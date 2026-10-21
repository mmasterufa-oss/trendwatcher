#!/usr/bin/env bash
# Установка трендвотчера на чистый VPS (Ubuntu 22.04/24.04).
# Запуск: curl -fsSL https://raw.githubusercontent.com/mmasterufa-oss/trendwatcher/main/install.sh | sudo bash
set -euo pipefail

REPO="https://github.com/mmasterufa-oss/trendwatcher.git"
DIR=/opt/trendwatcher
ENV_FILE=$DIR/.env

echo "== 0/4 Память =="
# Если памяти меньше 2 ГБ и нет swap — добавляем swap-файл на 2 ГБ (браузеру нужна память)
if [ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 2000000 ] && [ "$(awk '/SwapTotal/{print $2}' /proc/meminfo)" -lt 1000000 ]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "Добавлен swap 2 ГБ"
fi

echo "== 1/4 Docker =="
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
command -v git >/dev/null || (apt-get update -qq && apt-get install -y -qq git)

echo "== 2/4 Код агента =="
if [ -d "$DIR/src/.git" ]; then
  git -C "$DIR/src" pull --ff-only
else
  mkdir -p "$DIR"
  git clone --depth 1 "$REPO" "$DIR/src"
fi
mkdir -p "$DIR/data"

echo "== 3/4 Настройки =="
if [ ! -f "$ENV_FILE" ]; then
  ask() { local v; read -r -p "$1: " v </dev/tty; echo "$v"; }
  asks() { local v; read -r -s -p "$1: " v </dev/tty; echo >&2; echo "$v"; }
  TG=$(asks "Токен Telegram-бота (ввод скрыт)")
  TL=$(ask "Логин (почта) Trendsee")
  TP=$(asks "Пароль Trendsee (ввод скрыт)")
  CT=$(asks "Токен Claude из 'claude setup-token' (можно пусто, ввод скрыт)")
  umask 077
  cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$TG
TRENDSEE_LOGIN=$TL
TRENDSEE_PASSWORD=$TP
CLAUDE_CODE_OAUTH_TOKEN=$CT
TZ=Europe/Moscow
EOF
  chmod 600 "$ENV_FILE"
else
  echo "Настройки уже есть: $ENV_FILE (чтобы поменять: nano $ENV_FILE, затем снова этот скрипт)"
fi

echo "== 4/4 Сборка и запуск (5-10 минут) =="
docker build -t trendwatcher "$DIR/src"
docker rm -f trendwatcher >/dev/null 2>&1 || true
docker run -d --name trendwatcher --restart unless-stopped \
  --env-file "$ENV_FILE" -v "$DIR/data:/data" --shm-size=512m trendwatcher

sleep 5
docker logs --tail 20 trendwatcher
echo
echo "Готово. Напишите боту /start, затем /now."
echo "Логи: docker logs -f trendwatcher   Перезапуск: docker restart trendwatcher"
