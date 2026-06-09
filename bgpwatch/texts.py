"""Русские тексты и форматирование сообщений (HTML parse_mode)."""
import html


def esc(v) -> str:
    """HTML-экранирование любого подставляемого значения (defence-in-depth)."""
    return html.escape(str(v), quote=False)


def code(v) -> str:
    """Значение в <code> — Telegram не превращает IP/подсети в ссылки."""
    return f"<code>{esc(v)}</code>"


def bcode(v) -> str:
    """Жирный моноширинный — заметно и без авто-ссылки."""
    return f"<b><code>{esc(v)}</code></b>"

WELCOME = (
    "👋 Привет! Я <b>BGP Watch</b> — слежу, чтобы твои серверы были на связи.\n\n"
    "Даёшь мне IP — я каждые <b>15 минут</b> проверяю его двумя независимыми "
    "способами и сразу пишу, если что-то изменилось:\n"
    "• 🔌 <b>TCP-проба</b> — принимает ли хост соединение\n"
    "• 🌍 <b>BGP-видимость</b> — виден ли префикс в мировой таблице маршрутов "
    "(данные RIPE RIS)\n\n"
    "Сообщу и когда сервер <b>упал</b>, и когда <b>вернулся</b> — не нужно "
    "проверять вручную.\n\n"
    "<b>С чего начать:</b>\n"
    "<code>/watch 1.1.1.1</code> — добавить сервер под наблюдение\n\n"
    "<b>Все команды:</b>\n"
    "/watch <code>&lt;ip&gt; [порт]</code> — следить за хостом (порт по умолчанию 22)\n"
    "/watch <code>&lt;ip&gt;/24</code> — следить за подсетью (только BGP, без TCP)\n"
    "/unwatch <code>&lt;ip|подсеть&gt;</code> — перестать следить\n"
    "/list — сводка + только лежащие\n"
    "/listfull — полный список всех подписок\n"
    "/status <code>&lt;ip|подсеть&gt;</code> — проверить прямо сейчас\n"
    "/help — справка"
)

HELP = (
    "<b>Команды BGP Watch:</b>\n\n"
    "/watch <code>&lt;ip&gt; [порт]</code> — следить за хостом (TCP+BGP).\n"
    "   Порт по умолчанию 22. Пример: <code>/watch 1.1.1.1</code>\n"
    "/watch <code>&lt;подсеть&gt;</code> — следить за диапазоном (только BGP).\n"
    "   Пример: <code>/watch 185.157.212.0/24</code>\n"
    "/unwatch <code>&lt;ip|подсеть&gt;</code> — снять с наблюдения.\n"
    "/list — сводка (up/down) + только лежащие.\n"
    "/listfull — полный список всех подписок.\n"
    "/status <code>&lt;ip|подсеть&gt;</code> — разовая проверка прямо сейчас.\n"
    "/help — эта справка.\n\n"
    "<b>Статусы:</b>\n"
    "🟢 — хост отвечает / префикс виден\n"
    "🔴 — хост не отвечает / префикс не виден\n"
    "⚪ — нет данных (временный сбой проверки)"
)

UNKNOWN = "Не понял команду. /help — список команд."
BAD_IP = "Нужен корректный IP. Пример: <code>/watch 1.1.1.1</code>"
BAD_PORT = "Порт должен быть числом 1–65535. Пример: <code>/watch 1.2.3.4 443</code>"
NEED_IP = "Укажи IP. Пример: <code>/status 1.1.1.1</code>"
EMPTY_LIST = "Список пуст. Добавь IP командой /watch <code>1.2.3.4</code>"
PRIVATE_IP = (
    "Можно следить только за публичными IP. "
    "Приватные/служебные адреса (loopback, 10/172/192.168, 169.254, multicast) запрещены."
)
BAD_CIDR = (
    "Нужна корректная публичная подсеть. Пример: <code>/watch 185.157.212.0/24</code>"
)
RATE_LIMITED = "Слишком часто. Подожди немного и повтори."


def limit_reached(limit) -> str:
    return (
        f"Достигнут лимит подписок ({limit}). "
        f"Сними лишние через /unwatch и попробуй снова."
    )


def tcp_emoji(tcp: str) -> str:
    return "🟢" if tcp == "open" else "🔴"


def tcp_line(tcp: str, port: int) -> str:
    word = "открыт" if tcp == "open" else "закрыт"
    return f"{tcp_emoji(tcp)} TCP:{port} {word}"


def bgp_line(seeing, total) -> str:
    if total:
        pct = round((seeing or 0) / total * 100)
        em = "🟢" if pct >= 50 else "🔴"
        return f"{em} BGP {seeing}/{total} ({pct}%)"
    return "⚪ BGP: нет данных"


def asn_suffix(asn) -> str:
    return f" · AS{esc(asn)}" if asn else ""


def watch_added(ip, port, prefix, asn, tcp, seeing, total) -> str:
    if tcp is None:  # BGP-only (подсеть)
        return (
            f"✅ Слежу за подсетью {bcode(prefix)}{asn_suffix(asn)}\n"
            f"<i>режим BGP-only — TCP не проверяется</i>\n\n"
            f"Текущий статус:\n"
            f"{bgp_line(seeing, total)}\n\n"
            f"Сообщу при изменении видимости."
        )
    return (
        f"✅ Слежу за {bcode(ip)} (порт {esc(port)})\n"
        f"{code(prefix)}{asn_suffix(asn)}\n\n"
        f"Текущий статус:\n"
        f"{tcp_line(tcp, port)}\n"
        f"{bgp_line(seeing, total)}\n\n"
        f"Сообщу при изменении."
    )


def status_msg(ip, port, prefix, asn, tcp, seeing, total) -> str:
    if tcp is None:  # BGP-only
        return (
            f"📊 Подсеть {bcode(prefix)}{asn_suffix(asn)}\n"
            f"<i>BGP-only</i>\n\n"
            f"{bgp_line(seeing, total)}"
        )
    return (
        f"📊 {bcode(ip)} (порт {esc(port)})\n"
        f"{code(prefix)}{asn_suffix(asn)}\n\n"
        f"{tcp_line(tcp, port)}\n"
        f"{bgp_line(seeing, total)}"
    )


def list_header(total, up, down, unknown) -> str:
    line = f"🟢 up {up} · 🔴 down {down}"
    if unknown:
        line += f" · ⚪ ? {unknown}"
    return f"<b>Мои подписки: {total}</b>\n{line}"


def list_all_ok(total) -> str:
    return f"<b>Мои подписки: {total}</b>\n🟢 Все на связи — лежащих нет."


def list_problems_caption(shown) -> str:
    return f"\n<i>Показаны только лежащие ({shown}). Полный список — /listfull</i>"


def list_full_caption() -> str:
    return "\n<i>Все подписки:</i>"


def list_more(n) -> str:
    return f"<i>…и ещё {n} лежащих (полное число — в сводке выше).</i>"


def list_row(w, st) -> str:
    """w: watch row, st: state row|None."""
    bgp_only = w["port"] == 0
    label = code(w["prefix"]) if bgp_only else code(f"{w['ip']}:{w['port']}")
    if st is None:
        return f"• {label} — ещё не проверен"
    bgp_em = bgp_line(st["bgp_seeing"], st["bgp_total"]).split(" ", 1)[0]
    short = _short_bgp(st["bgp_seeing"], st["bgp_total"])
    if bgp_only:
        return f"• {label}  {bgp_em}BGP {short}"
    tcp = st["tcp"] or "closed"
    return (
        f"• {label}  "
        f"{tcp_emoji(tcp)}TCP  "
        f"{bgp_em}BGP {short}"
    )


def _short_bgp(seeing, total) -> str:
    if total:
        return f"{round((seeing or 0)/total*100)}%"
    return "—"


def event_msg(ip, port, prefix, asn, tcp, seeing, total, events) -> str:
    bgp_only = tcp is None
    improved = any(e["to"] in ("open", "up") for e in events)
    degraded = any(e["to"] in ("closed", "down") for e in events)
    label = bcode(prefix if bgp_only else ip)
    if degraded and not improved:
        head = f"🔴 {label} — упал"
    elif improved and not degraded:
        head = f"🟢 {label} — вернулся"
    else:
        head = f"🟡 {label} — изменение статуса"

    changes = []
    for e in events:
        if e["kind"] == "tcp":
            changes.append(f"TCP:{port} {e['from']} → {e['to']}")
        else:
            changes.append(f"BGP {e['from']} → {e['to']}")

    lines = [head]
    if not bgp_only:
        lines.append(f"{code(prefix)}{asn_suffix(asn)}")
    lines.append("")
    if not bgp_only:
        lines.append(tcp_line(tcp, port))
    lines.append(bgp_line(seeing, total))
    lines.append("")
    lines.append(f"<i>{esc(', '.join(changes))}</i>")
    return "\n".join(lines)
