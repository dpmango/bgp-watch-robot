#!/usr/bin/env bash
# Деплой BGP Watch на сервер. Запуск локально:
#   ./deploy/deploy.sh root@1.2.3.4
# Токен НЕ передаётся скриптом — .env создаётся на сервере вручную (см. README).
set -euo pipefail

REMOTE="${1:?usage: deploy.sh user@host}"
APP_DIR=/opt/bgpwatch
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> rsync кода в $REMOTE:$APP_DIR"
ssh "$REMOTE" "mkdir -p $APP_DIR"
rsync -az --delete \
  --exclude '.venv' --exclude '.env' --exclude '*.db*' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.git' \
  "$SRC/bgpwatch" "$SRC/deploy" "$SRC/requirements.txt" "$REMOTE:$APP_DIR/"

echo ">> настройка пользователя, venv, зависимостей, сервиса"
ssh "$REMOTE" bash -s <<'REMOTE_EOF'
set -euo pipefail
APP_DIR=/opt/bgpwatch
id bgpwatch &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin bgpwatch
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
install -m 644 "$APP_DIR/deploy/bgpwatch.service" /etc/systemd/system/bgpwatch.service
chown -R bgpwatch:bgpwatch "$APP_DIR"
systemctl daemon-reload
echo "Готово. Создай $APP_DIR/.env (chmod 600), затем: systemctl enable --now bgpwatch"
REMOTE_EOF
