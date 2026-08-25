# BGP Watch — Telegram-бот мониторинга доступности серверов

Создано: 2026-06-09 · Обновлено: 2026-08-25 · Бот: **@BGPWatchRobot**
Деплой: `root@v3116877.hosted-by-vdsina.ru` (`/opt/bgpwatch`, systemd)

## Цель

Пользователь отправляет боту IP-адрес или подсеть. Бот каждые 15 минут проверяет
доступность и сообщает при **любом изменении** статуса (упал / вернулся).

## Сигналы

| Сигнал | Источник | Что меряет |
|---|---|---|
| **TCP-проба** | `asyncio.open_connection(ip, port)` с нашего сервера | принимает ли конкретный хост соединение (ground truth, как `ssh`) |
| **BGP-видимость** | RIPEstat `routing-status` API (`ris_peers_seeing/total_ris_peers`) | виден ли префикс в мировой BGP-таблице (RIPE RIS) |

Порог BGP: `ratio >= 0.5` → **up**, иначе **down**. Экстремумы чистые: видимый
префикс = `326/326`, снятый = `1/326`.

Сигналы независимы: хост может быть BGP-up но ssh лёг, и наоборот → следим за
обоими. Алерт приходит при изменении **любого** из них, в **обе стороны**.

### Два режима подписки

- **Хост** (`/watch 1.2.3.4 [порт]`) — TCP-проба + BGP. `port` хранится как есть.
- **Подсеть / BGP-only** (`/watch 1.2.3.0/24`) — только BGP-видимость префикса,
  **без TCP** (на адресе сети никто не слушает → TCP был бы вечно ложно-`closed`).
  В БД помечается `port = 0`, `ip = адрес сети`, `prefix = CIDR`.

## Почему так

- **RIPEstat вместо парсинга he.net**: чистый JSON, без Cloudflare/JS-рендера.
  Лимиты RIPEstat: нет жёсткого лимита на число запросов (регистрация `sourceapp`
  при >1000/день), максимум 8 одновременных. Знаменатель (~326 RIS-пиров)
  отличается от he.net (~860) — то же BGP-событие, другая выборка; up/down совпадает.
- **Long-polling, не webhook**: на сервере заняты :80/:443 (nginx). Polling не
  требует порта/cert.
- **Только httpx, без aiogram**: Python 3.14 на сервере; избегаем C-зависимостей
  (aiohttp), минимизируем поверхность атаки. Свой минимальный TG-клиент.

## Telegram под РФ-блокировкой (самолечение)

`api.telegram.org` на этом хостинге доступен лишь по части IP (DNS отдаёт
null-route'нутый `149.154.166.110`; реально проходит `149.154.167.220`).
`telegram.py` коннектится по **явному IP** с `sni_hostname=api.telegram.org`
(TLS-серт проверяется корректно). Если все известные IP отвалились — **сканирует
диапазоны** (`TELEGRAM_DISCOVER_RANGES`), находит рабочий bot-API хост, валидирует
`getMe`, переключается и запоминает в `telegram_ip.txt` (кулдаун скана 120с).
Дополнительно `/etc/hosts` пинит рабочий IP.

## Анти-флаппинг

**BGP.** Сбой фетча RIPEstat (`http=000`/timeout/429) = «нет данных» (unknown).
Держим прошлый BGP-статус, **не алертим**. Лечится 3 ретраями. Алерт только на
подтверждённый переход состояния. Первый замер подписки = тихий baseline.

**TCP.** Одна проба вердикта не даёт: провалившиеся цели перепроверяются
(`PROBE_ATTEMPTS=3` с паузой `PROBE_RETRY_DELAY=5`с), успешные — нет, поэтому в
норме ретрай ничего не стоит. Если провалы остались, цикл сверяется с
**контрольными хостами** (`config.ANCHORS` — Cloudflare/Quad9/Yandex/OpenDNS,
проверяются только при наличии провалов). Недоступны все сразу → легла наша сеть,
а не наблюдаемые хосты: TCP в этом цикле = «нет данных», прошлый статус
сохраняется, событий нет. Проверка самозавершающаяся — как только связь
восстановлена, якоря отвечают и реальное падение доезжает до подписчика.

Зачем: наблюдение идёт с одной точки (RU-хостинг), и моргнувший аплинк без этой
защиты выглядел как синхронное падение чужих хостов — 12 серверов в 12 разных
префиксах «упали» и «вернулись» одним циклом.

## Модули (изолированные)

| Файл | Ответственность | Зависит от |
|---|---|---|
| `config.py` | чтение `.env` / env | — |
| `bgp.py` | RIPEstat: `ip_to_prefix`, `prefix_visibility`, парсеры (чистые) | httpx |
| `probe.py` | async TCP-проба, `probe_many` с лимитом конкурентности и ретраем провалов | asyncio |
| `store.py` | SQLite: `watches` + `state`, CRUD | sqlite3 |
| `monitor.py` | `compute_events` (чистая), `network_down` (якоря), `run_cycle` (проба+BGP+diff+нотификация) | bgp, probe, store |
| `texts.py` | русские тексты/форматирование (host и BGP-only) | — |
| `telegram.py` | минимальный async-клиент Bot API + IP-пиннинг/самопоиск | httpx |
| `bot.py` | разбор команд, rate-limit, polling-цикл, monitor-цикл | всё выше |
| `__main__.py` | точка входа | bot |

## Данные (SQLite)

```sql
watches(chat_id, ip, port, prefix, asn, added_at, PK(chat_id,ip,port))
state(ip, port, prefix, asn, tcp, bgp_seeing, bgp_total, last_change, last_check, PK(ip,port))
```

- `port = 0` → BGP-only подписка (TCP не проверяется, `state.tcp = NULL`).
- Мульти-юзер: несколько `chat_id` могут следить за одним `(ip,port)`. Пробы/BGP
  считаются один раз на уникальный target/prefix, нотификация — всем подписчикам.

## Команды

| Команда | Действие |
|---|---|
| `/start` | приветствие |
| `/help` | справка |
| `/watch <ip> [порт]` | следить за хостом (TCP+BGP), сразу показать статус |
| `/watch <CIDR>` | следить за подсетью (только BGP) |
| `/unwatch <ip\|CIDR>` | снять с наблюдения |
| `/list` | мои подписки + последний статус (пагинация при >3500 симв.) |
| `/status <ip\|CIDR>` | разовая проверка прямо сейчас (без сохранения) |

Меню регистрируется через `setMyCommands`. Все ответы на русском, HTML-экранированы.

## Цикл мониторинга (`run_cycle`)

1. Загрузить уникальные `(ip, port, prefix)` из `watches`.
2. TCP-пробы **параллельно** (semaphore `PROBE_CONCURRENCY`) — только для `port != 0`.
3. BGP по уникальным префиксам **параллельно** (semaphore `BGP_CONCURRENCY`, ≤8 у
   RIPEstat). Потолок `MAX_PREFIXES_PER_CYCLE` на цикл (защита от DoS).
4. При провалах — сверка с якорями; легла наша сеть → TCP всего цикла = «нет данных».
5. Для каждого target: `tcp` (`None` если `port==0` или сбой нашей сети), `bgp`.
6. `compute_events(old, tcp, bgp)` → переходы. `tcp=None` и `unknown` событий не
   дают; unknown переносит старые seeing/total и прошлый TCP-статус.
7. Сохранить state, отправить события всем подписчикам.

## Конфиг (`.env`, полный список — в `.env.example`)

```
BOT_TOKEN=...               # секрет, chmod 600, не в git
POLL_INTERVAL=900           # период цикла, сек
DEFAULT_PORT=22
PROBE_TIMEOUT=4 / PROBE_CONCURRENCY=100
PROBE_ATTEMPTS=3 / PROBE_RETRY_DELAY=5   # подтверждение провалившейся пробы
BGP_UP_THRESHOLD=0.5 / BGP_CONCURRENCY=5
RIPE_SOURCEAPP=bgpwatchrobot / RIPE_TIMEOUT=25 / RIPE_RETRIES=3
MAX_WATCHES_PER_CHAT=50 / MAX_PREFIXES_PER_CYCLE=500
RATE_LIMIT_CMDS=20 / RATE_LIMIT_WINDOW=60 / RATE_LIMIT_MAX_CHATS=10000
UNLIMITED_IDS=             # chat_id без лимитов и rate-limit (через запятую)
TELEGRAM_API_IPS=149.154.167.220
TELEGRAM_DISCOVER_RANGES=... / TELEGRAM_IP_STATE=...
```

## Деплой

- `/opt/bgpwatch`, системный пользователь `bgpwatch` (без логина).
- venv с системным Python 3.14, `pip install httpx`.
- systemd-юнит `bgpwatch.service`: `Restart=always`, `MemoryMax=200M`, харденинг
  (`NoNewPrivileges`, `ProtectSystem=strict`, `RestrictAddressFamilies` с
  `AF_UNIX/AF_NETLINK` для DNS, `SystemCallFilter`, снятые capabilities и др.).
- `.env` создаётся на сервере вручную (токен не в репозитории).
- `/etc/hosts`: `149.154.167.220 api.telegram.org`.

## Нагрузка (1 vCPU / 956MB)

- Память процесса: ~24MB (замер). Лимит 200MB.
- BGP параллельно (conc=5) + пробы параллельно. Замер: **263 цели → ~59с/цикл**.
- Потолок `MAX_PREFIXES_PER_CYCLE=500`; реально 500 префиксов при conc=5 ≈ ~2–3 мин,
  с запасом в 900с. RIPEstat: `P×96` запросов/день (`sourceapp` зарегистрирован).
- Реальная нагрузка — доли процента CPU (≈1с CPU за ~2ч).

## Тесты (`pytest`, 45)

- `bgp`: парсеры `parse_network_info`/`parse_routing_status`, `fallback_prefix`.
- `monitor`: `compute_events` (все переходы, unknown, baseline, BGP-only),
  `run_cycle` (host и BGP-only, пропуск пробы при `port=0`), подавление массового
  ложного алерта при отказе якорей и сохранение реального одиночного падения.
- `probe`: `tcp_probe` против локального порта, ретрай провала, отсутствие
  лишних проб при успехе.
- `store`: CRUD, счётчики.
- `security`: `public_ip`/`public_net` (SSRF-блок), rate-limit + GC, HTML-escape
  в `_unwatch`, лимит подписок, UNLIMITED bypass.
- `telegram`: IP-пиннинг URL, failover между IP, авто-discovery.

## Безопасность

Базовое:
- Токен только в `.env` (chmod 600), не в git. httpx/httpcore-логи → WARNING,
  чтобы URL с токеном не попадал в journald.
- ⚠️ Токен был прислан в чат открытым текстом → **перевыпустить** в @BotFather.

Проведён security-аудит (отдельный процесс). Исправлено:
- **SSRF (CRITICAL)** — `public_ip()`/`public_net()` отклоняют loopback/RFC1918/
  link-local (169.254 metadata)/reserved/multicast/unspecified и IPv4-mapped IPv6.
  Применяется в `/watch`, `/status`, `/unwatch` (host и CIDR) до любой TCP-пробы.
- **DoS/abuse (CRITICAL/HIGH)** — `MAX_WATCHES_PER_CHAT=50`, rate-limit
  `RATE_LIMIT_CMDS=20`/60с на чат (+GC словаря), потолок `MAX_PREFIXES_PER_CYCLE=500`.
  Whitelist `UNLIMITED_IDS` снимает лимиты для доверенных chat_id.
- **systemd-хардненинг (HIGH)** — seccomp, `RestrictAddressFamilies`, снятые caps,
  `ProtectSystem=strict` и др. Проверено: DNS + RIPEstat работают под sandbox.
- **HTML-инъекция (MEDIUM)** — `html.escape()` на всех подставляемых значениях;
  `prefix` из RIPEstat валидируется `ip_network`; `_unwatch` экранирует ввод.
- **/list** пагинируется (лимит Telegram 4096).
- **Токен в URL** — обход блокировки через IP-пиннинг не логирует токен.

SQL-инъекций нет (параметризовано). Краш-путей от пользовательского ввода нет.

Опционально (не включено): `IPAddressDeny` приватных диапазонов в юните
(ломает DNS через 127.0.0.53 — SSRF уже закрыт в коде).
