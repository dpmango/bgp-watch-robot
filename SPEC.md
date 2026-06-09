# BGP Watch — Telegram-бот мониторинга доступности серверов

Дата: 2026-06-09 · Бот: **@BGPWatchRobot** · Деплой: `root@1.2.3.4`

## Цель

Пользователь отправляет боту IP-адрес. Бот каждые 15 минут проверяет его двумя
независимыми способами и сообщает при **любом изменении** статуса (упал / вернулся).

## Два сигнала

| Сигнал | Источник | Что меряет |
|---|---|---|
| **TCP-проба** | `asyncio.open_connection(ip, port)` с нашего сервера | принимает ли конкретный хост соединение (ground truth, как `ssh`) |
| **BGP-видимость** | RIPEstat `routing-status` API (`ris_peers_seeing/total_ris_peers`) | виден ли /24-префикс в мировой BGP-таблице (RIPE RIS) |

Порог BGP: `ratio >= 0.5` → **up**, иначе **down**. Экстремумы чистые: видимый
префикс = `326/326`, снятый = `1/326`.

Слои независимы: хост может быть BGP-up но ssh лёг, и наоборот → следим за обоими.

## Почему так

- **RIPEstat вместо парсинга he.net**: чистый JSON, без Cloudflare, без JS-рендера.
  Лимиты RIPEstat: нет жёсткого лимита на число запросов (регистрация `sourceapp`
  при >1000/день), максимум 8 одновременных. Идём последовательно с интервалом.
  Знаменатель (~326 RIS-пиров) отличается от he.net (~860 сессий) — то же
  BGP-событие, другая выборка; up/down совпадает.
- **Long-polling, не webhook**: на сервере заняты :80/:443 (nginx). Polling не
  требует порта/cert.
- **Только httpx, без aiogram**: Python 3.14 на сервере; избегаем C-зависимостей
  (aiohttp), минимизируем поверхность атаки. Свой минимальный TG-клиент.

## Анти-флаппинг

Сбой фетча RIPEstat (`http=000`/timeout/429) = «нет данных» (unknown). Держим
прошлый BGP-статус, **не алертим**. RIPEstat периодически роняет запрос — это
норма, лечится 3 ретраями. Алерт только на подтверждённый переход состояния.

## Модули (изолированные)

| Файл | Ответственность | Зависит от |
|---|---|---|
| `config.py` | чтение `.env` / env | — |
| `bgp.py` | RIPEstat: `ip_to_prefix`, `prefix_visibility`, парсеры (чистые ф-ии) | httpx |
| `probe.py` | async TCP-проба, `probe_many` с лимитом конкурентности | asyncio |
| `store.py` | SQLite: `watches` + `state`, CRUD | sqlite3 |
| `monitor.py` | `compute_events` (чистая), `run_cycle` (проба+BGP+diff+нотификация) | bgp, probe, store |
| `texts.py` | русские тексты/форматирование сообщений | — |
| `telegram.py` | минимальный async-клиент Bot API над httpx | httpx |
| `bot.py` | разбор команд, polling-цикл, monitor-цикл | всё выше |
| `__main__.py` | точка входа | bot |

## Данные (SQLite)

```sql
watches(chat_id, ip, port, prefix, asn, added_at, PK(chat_id,ip,port))
state(ip, port, prefix, asn, tcp, bgp_seeing, bgp_total, last_change, last_check, PK(ip,port))
```

Мульти-юзер: несколько `chat_id` могут следить за одним IP. Пробы/BGP считаются
один раз на уникальный `(ip,port)` / `prefix`, нотификация уходит всем подписчикам.

## Команды

| Команда | Действие |
|---|---|
| `/start` | приветствие + краткая инструкция |
| `/help` | справка |
| `/watch <ip> [порт]` | добавить под наблюдение (порт по умолчанию 22), сразу показать текущий статус |
| `/unwatch <ip>` | снять с наблюдения |
| `/list` | мои IP + их последний статус |
| `/status <ip>` | разовая проверка прямо сейчас (проба + BGP), без сохранения |

Меню команд регистрируется через `setMyCommands`. Все ответы на русском.

## Цикл мониторинга

1. Загрузить все уникальные `(ip, port, prefix)` из `watches`.
2. Пробить все `(ip,port)` параллельно (semaphore, лимит `PROBE_CONCURRENCY`).
3. Запросить BGP по уникальным префиксам **последовательно** с паузой `RIPE_SPACING`.
4. Для каждого target: новый `tcp` (open/closed), новый `bgp` (up/down/unknown).
5. `compute_events(old_state, tcp, bgp)` → переходы. unknown BGP не генерит событий,
   переносит старые seeing/total.
6. Сохранить state, отправить события всем подписчикам.

## Конфиг (`.env`)

```
BOT_TOKEN=...            # секрет, chmod 600, не в git
POLL_INTERVAL=900        # 15 мин
DEFAULT_PORT=22
PROBE_TIMEOUT=4
PROBE_CONCURRENCY=100
BGP_UP_THRESHOLD=0.5
RIPE_SOURCEAPP=bgpwatchrobot
RIPE_SPACING=1.5
RIPE_RETRIES=3
```

## Деплой

- `/opt/bgpwatch`, системный пользователь `bgpwatch` (без логина).
- venv с системным Python 3.14, `pip install httpx`.
- systemd-юнит `bgpwatch.service`: `Restart=always`, `MemoryMax=200M`, харденинг
  (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`).
- `.env` создаётся на сервере вручную (токен не в репозитории).

## Оценка нагрузки (1 vCPU / 956MB)

- Память процесса: ~40–70MB (httpx + asyncio). Лимит 200MB.
- За цикл: `W` проб параллельно (≤ неск. сек при concurrency 100) + `P` BGP-запросов
  последовательно (~`RIPE_SPACING` с каждый).
- Потолок BGP: `P * RIPE_SPACING < POLL_INTERVAL` → ~600 префиксов теоретически,
  комфортно 100–150. Пробы 1000 IP при concurrency 100 → ~30с худший случай.
- Реальная нагрузка (десятки серверов) — доли процента CPU, простаивающий бот.

## Тесты

- `parse_network_info`, `parse_routing_status` (чистые парсеры на фикстурах JSON).
- `compute_events` (все переходы: open↔closed, up↔down, unknown, первый запуск).
- `tcp_probe` против локального открытого/закрытого порта.
- `Store` CRUD на временной БД.

## Безопасность

Базовое:
- Токен только в `.env` (chmod 600), не в git. httpx/httpcore-логи заглушены до
  WARNING, чтобы URL с токеном не попадал в journald.
- ⚠️ Токен был прислан в чат открытым текстом → **перевыпустить** в @BotFather.

Проведён security-аудит (отдельный процесс). Исправлено:
- **SSRF (CRITICAL)** — `public_ip()` в `bot.py` отклоняет loopback/RFC1918/
  link-local (169.254 metadata)/reserved/multicast/unspecified и IPv4-mapped IPv6.
  Применяется в `/watch` и `/status` до любой TCP-пробы. Бот не может пробить
  внутреннюю сеть хостинга или cloud-metadata.
- **DoS/abuse (CRITICAL/HIGH)** — лимит `MAX_WATCHES_PER_CHAT=50`, rate-limit
  `RATE_LIMIT_CMDS=20`/60с на чат, потолок `MAX_PREFIXES_PER_CYCLE=300` на цикл.
- **systemd-хардненинг (HIGH)** — `RestrictAddressFamilies` (с `AF_UNIX/AF_NETLINK`
  для DNS), `SystemCallFilter=@system-service ~@privileged @resources`,
  снятые capabilities, `ProtectSystem=strict`, `PrivateDevices`, `RestrictNamespaces`
  и др. Проверено: DNS + RIPEstat работают под sandbox.
- **HTML-инъекция (MEDIUM)** — `html.escape()` на всех подставляемых значениях
  (ip/prefix/asn) в `texts.py`; `prefix` из RIPEstat валидируется `ip_network`.
- **/list** пагинируется (лимит Telegram 4096).
- **deploy.sh** — `deploy/` включён в rsync, юнит ставится через `install`, без
  тихого `|| true`.

SQL-инъекций нет (всё параметризовано). Краш-путей от пользовательского ввода нет.

Опционально (не включено): `IPAddressDeny` приватных диапазонов в юните
(ломает DNS через 127.0.0.53 — SSRF уже закрыт в коде).
