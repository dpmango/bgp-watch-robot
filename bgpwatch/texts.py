"""Русские тексты и форматирование сообщений (HTML parse_mode)."""
import html


def esc(v) -> str:
    """HTML-экранирование любого подставляемого значения (defence-in-depth)."""
    return html.escape(str(v), quote=False)

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
    "/watch <code>&lt;ip&gt; [порт]</code> — следить (порт по умолчанию 22)\n"
    "/unwatch <code>&lt;ip&gt;</code> — перестать следить\n"
    "/list — мои серверы и их статус\n"
    "/status <code>&lt;ip&gt;</code> — проверить прямо сейчас\n"
    "/help — справка"
)

HELP = (
    "<b>Команды BGP Watch:</b>\n\n"
    "/watch <code>&lt;ip&gt; [порт]</code> — добавить IP под наблюдение.\n"
    "   Порт по умолчанию 22. Пример: <code>/watch 1.1.1.1</code>\n"
    "/unwatch <code>&lt;ip&gt;</code> — снять IP с наблюдения.\n"
    "/list — список моих IP с последним статусом.\n"
    "/status <code>&lt;ip&gt;</code> — разовая проверка прямо сейчас.\n"
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
    return (
        f"✅ Слежу за <b>{esc(ip)}</b> (порт {esc(port)})\n"
        f"<code>{esc(prefix)}</code>{asn_suffix(asn)}\n\n"
        f"Текущий статус:\n"
        f"{tcp_line(tcp, port)}\n"
        f"{bgp_line(seeing, total)}\n\n"
        f"Сообщу при изменении."
    )


def status_msg(ip, port, prefix, asn, tcp, seeing, total) -> str:
    return (
        f"📊 <b>{esc(ip)}</b> (порт {esc(port)})\n"
        f"<code>{esc(prefix)}</code>{asn_suffix(asn)}\n\n"
        f"{tcp_line(tcp, port)}\n"
        f"{bgp_line(seeing, total)}"
    )


def list_row(w, st) -> str:
    """w: watch row, st: state row|None."""
    ipp = f"{esc(w['ip'])}:{esc(w['port'])}"
    if st is None:
        return f"• <b>{ipp}</b> — ещё не проверен"
    tcp = st["tcp"] or "closed"
    return (
        f"• <b>{ipp}</b>  "
        f"{tcp_emoji(tcp)}TCP  "
        f"{bgp_line(st['bgp_seeing'], st['bgp_total']).split(' ', 1)[0]}BGP "
        f"{_short_bgp(st['bgp_seeing'], st['bgp_total'])}"
    )


def _short_bgp(seeing, total) -> str:
    if total:
        return f"{round((seeing or 0)/total*100)}%"
    return "—"


def event_msg(ip, port, prefix, asn, tcp, seeing, total, events) -> str:
    improved = any(e["to"] in ("open", "up") for e in events)
    degraded = any(e["to"] in ("closed", "down") for e in events)
    if degraded and not improved:
        head = f"🔴 <b>{esc(ip)}</b> — упал"
    elif improved and not degraded:
        head = f"🟢 <b>{esc(ip)}</b> — вернулся"
    else:
        head = f"🟡 <b>{esc(ip)}</b> — изменение статуса"

    changes = []
    for e in events:
        if e["kind"] == "tcp":
            changes.append(f"TCP:{port} {e['from']} → {e['to']}")
        else:
            changes.append(f"BGP {e['from']} → {e['to']}")

    return (
        f"{head}\n"
        f"<code>{esc(prefix)}</code>{asn_suffix(asn)}\n\n"
        f"{tcp_line(tcp, port)}\n"
        f"{bgp_line(seeing, total)}\n\n"
        f"<i>{esc(', '.join(changes))}</i>"
    )
