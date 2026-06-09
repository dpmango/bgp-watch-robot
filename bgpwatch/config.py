"""Конфигурация: читает .env (рядом с пакетом) и переменные окружения."""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    """Простой парсер .env. Без внешних зависимостей. env имеет приоритет."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env(_ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", str(_ROOT / "bgpwatch.db"))

# --- Telegram IP-пиннинг (обход РФ-блокировки api.telegram.org) ---
# Список рабочих bot-API IP (через них коннект с SNI=api.telegram.org). Пусто →
# обычный DNS-резолв хоста.
TELEGRAM_API_IPS = [
    x for x in os.environ.get("TELEGRAM_API_IPS", "149.154.167.220")
    .replace(" ", "").split(",") if x
]
# Диапазоны для авто-поиска нового рабочего IP, если все из списка отвалились.
TELEGRAM_DISCOVER_RANGES = [
    x for x in os.environ.get(
        "TELEGRAM_DISCOVER_RANGES",
        "149.154.167.0/24,149.154.166.0/24,149.154.175.0/24,"
        "91.108.56.0/24,91.108.4.0/24",
    ).replace(" ", "").split(",") if x
]
# Файл с последним рабочим IP (переживает рестарт).
TELEGRAM_IP_STATE = os.environ.get("TELEGRAM_IP_STATE", str(_ROOT / "telegram_ip.txt"))

POLL_INTERVAL = _int("POLL_INTERVAL", 900)          # период мониторинга, сек (15 мин)
DEFAULT_PORT = _int("DEFAULT_PORT", 22)             # порт TCP-пробы по умолчанию
PROBE_TIMEOUT = _float("PROBE_TIMEOUT", 4.0)        # таймаут одной пробы, сек
PROBE_CONCURRENCY = _int("PROBE_CONCURRENCY", 100)  # макс. параллельных проб

BGP_UP_THRESHOLD = _float("BGP_UP_THRESHOLD", 0.5)  # доля пиров для статуса "up"

RIPE_SOURCEAPP = os.environ.get("RIPE_SOURCEAPP", "bgpwatchrobot")
RIPE_TIMEOUT = _float("RIPE_TIMEOUT", 25.0)         # таймаут BGP-запроса, сек
RIPE_RETRIES = _int("RIPE_RETRIES", 3)              # ретраи при http=000/429

# Параллельность BGP-запросов за цикл (RIPEstat допускает до 8 одновременных).
BGP_CONCURRENCY = _int("BGP_CONCURRENCY", 5)

# --- лимиты (защита от DoS/abuse публичного бота) ---
MAX_WATCHES_PER_CHAT = _int("MAX_WATCHES_PER_CHAT", 50)  # макс. подписок на чат
RATE_LIMIT_CMDS = _int("RATE_LIMIT_CMDS", 20)            # команд за окно на чат
RATE_LIMIT_WINDOW = _float("RATE_LIMIT_WINDOW", 60.0)    # окно rate-limit, сек
MAX_PREFIXES_PER_CYCLE = _int("MAX_PREFIXES_PER_CYCLE", 500)  # потолок BGP/цикл
RATE_LIMIT_MAX_CHATS = _int("RATE_LIMIT_MAX_CHATS", 10000)    # порог GC dict rate-limit

# chat_id без лимитов на добавление и без rate-limit (через запятую).
UNLIMITED_IDS = {
    int(x) for x in os.environ.get("UNLIMITED_IDS", "").replace(" ", "").split(",") if x
}
