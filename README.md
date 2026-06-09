# BGP Watch — @BGPWatchRobot

Telegram-бот: следит за доступностью серверов по IP двумя сигналами —
**TCP-проба** (принимает ли хост соединение) и **BGP-видимость** префикса
(RIPEstat / RIPE RIS). Пишет при любом изменении статуса. Проверка каждые 15 минут.

Подробности и обоснование решений — в [SPEC.md](SPEC.md).

## Команды

| Команда | Действие |
|---|---|
| `/start` | приветствие |
| `/watch <ip> [порт]` | следить (порт по умолчанию 22) |
| `/unwatch <ip>` | перестать следить |
| `/list` | мои IP + статус |
| `/status <ip>` | проверить сейчас |
| `/help` | справка |

## Локальная разработка

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q          # тесты
cp .env.example .env                    # вписать BOT_TOKEN
.venv/bin/python -m bgpwatch            # запуск
```

## Деплой (Ubuntu, systemd)

```bash
./deploy/deploy.sh root@SERVER
# затем на сервере:
#   printf 'BOT_TOKEN=XXX\n' > /opt/bgpwatch/.env && chmod 600 /opt/bgpwatch/.env
#   chown bgpwatch:bgpwatch /opt/bgpwatch/.env
#   cp /opt/bgpwatch/deploy/bgpwatch.service /etc/systemd/system/
#   systemctl enable --now bgpwatch
#   journalctl -u bgpwatch -f
```

Используется **long-polling** — порт/cert/nginx не нужны.

## Стек

Python 3.11+, единственная зависимость — `httpx` (чистый Python, без компиляции).
TG-клиент минимальный (`bgpwatch/telegram.py`), без aiogram/aiohttp.

## Безопасность

- `BOT_TOKEN` только в `.env` (chmod 600), не в git, не в логах.
- При утечке токена — `/revoke` в @BotFather.
- Бот публичный: любой пользователь может следить за любым IP.
