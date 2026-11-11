#!/usr/bin/env bash
# Деплой BGP Watch на сервер. Запуск локально:
#   ./deploy/deploy.sh root@1.2.3.4
# Токен НЕ передаётся скриптом — .env создаётся на сервере вручную (см. README).
set -euo pipefail

REMOTE="${1:?usage: deploy.sh user@host}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

STAGE="$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" mktemp -d /var/tmp/bgpwatch-deploy.XXXXXXXX)"
trap 'deploy_status=$?; ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" rm -rf -- "$STAGE" || true; exit "$deploy_status"' EXIT

echo ">> подготовка релиза в $REMOTE:$STAGE"
rsync -az --delete \
  -e 'ssh -o BatchMode=yes -o ConnectTimeout=15' \
  --exclude '__pycache__' \
  "$SRC/bgpwatch" "$SRC/deploy" "$SRC/requirements.txt" "$REMOTE:$STAGE/"

echo ">> установка, проверка запуска и откат при ошибке"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE" python3 "$STAGE/deploy/apply.py" "$STAGE"
