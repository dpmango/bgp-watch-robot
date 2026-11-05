"""Оркестрация бота: разбор команд, polling-цикл, monitor-цикл."""
import asyncio
import ipaddress
import logging
import time

import httpx

from . import bgp, config, monitor, texts
from .store import Store
from .telegram import Telegram

log = logging.getLogger("bgpwatch")

COMMANDS = [
    {"command": "start", "description": "Запуск и приветствие"},
    {"command": "watch", "description": "Следить: /watch 1.2.3.4 [порт] или /watch 1.2.3.0/24"},
    {"command": "unwatch", "description": "Перестать следить: /unwatch 1.2.3.4"},
    {"command": "name", "description": "Название подписки: /name 1.2.3.4 основной"},
    {"command": "list", "description": "Сводка + только лежащие"},
    {"command": "listfull", "description": "Полный список всех подписок"},
    {"command": "status", "description": "Проверить IP сейчас: /status 1.2.3.4"},
    {"command": "help", "description": "Справка по командам"},
]


def valid_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def public_ip(s: str):
    """-> нормализованный str публичного IP, либо None.

    Отсекает loopback/RFC1918/link-local(169.254 metadata)/reserved/multicast/
    unspecified, разворачивает IPv4-mapped IPv6 (::ffff:127.0.0.1). Защита от SSRF:
    бот не должен пробить внутреннюю сеть хостинга или cloud-metadata-эндпоинт.
    """
    try:
        addr = ipaddress.ip_address(s)
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or not addr.is_global
    ):
        return None
    return str(addr)


def public_net(s: str):
    """CIDR -> (base_ip, cidr) для публичной подсети, иначе None.

    BGP-only подписка: следим за видимостью префикса, без TCP. Отсекает
    приватные/служебные диапазоны (та же SSRF-защита, что и для хоста).
    """
    if "/" not in s:
        return None
    try:
        net = ipaddress.ip_network(s, strict=False)
    except ValueError:
        return None
    base = net.network_address
    if (
        base.is_private
        or base.is_loopback
        or base.is_link_local
        or base.is_reserved
        or base.is_multicast
        or base.is_unspecified
        or not base.is_global
    ):
        return None
    return str(base), str(net)


def parse_port(s: str):
    try:
        port = int(s)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


class Bot:
    def __init__(self):
        if not config.BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN не задан (.env)")
        self.tg = Telegram(
            config.BOT_TOKEN,
            ips=config.TELEGRAM_API_IPS,
            discover_ranges=config.TELEGRAM_DISCOVER_RANGES,
            state_path=config.TELEGRAM_IP_STATE,
        )
        self.store = Store(config.DB_PATH)
        self.http = httpx.AsyncClient()
        self._offset = 0
        self._rl: dict = {}  # chat_id -> [timestamps] для rate-limit

    def _rate_ok(self, chat_id) -> bool:
        if chat_id in config.UNLIMITED_IDS:
            return True
        now = time.monotonic()
        window = config.RATE_LIMIT_WINDOW
        hits = [t for t in self._rl.get(chat_id, []) if now - t < window]
        if len(hits) >= config.RATE_LIMIT_CMDS:
            self._rl[chat_id] = hits
            return False
        hits.append(now)
        self._rl[chat_id] = hits
        # GC: dict не должен расти неограниченно с числом уникальных чатов.
        # При превышении порога выкидываем ключи без активности в окне.
        if len(self._rl) > config.RATE_LIMIT_MAX_CHATS:
            self._rl = {
                cid: ts
                for cid, ts in self._rl.items()
                if any(now - t < window for t in ts)
            }
        return True

    # --- нотификация из monitor ---
    async def notify(self, chat_id, ip, port, prefix, asn, tcp, seeing, total, events):
        await self.tg.send_message(
            chat_id, texts.event_msg(
                ip, port, prefix, asn, tcp, seeing, total, events,
                self.store.get_label(chat_id, ip, port),
            )
        )

    # --- команды ---
    async def handle(self, msg: dict):
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if not self._rate_ok(chat_id):
            await self.tg.send_message(chat_id, texts.RATE_LIMITED)
            return
        try:
            if cmd == "/start":
                await self.tg.send_message(chat_id, texts.WELCOME)
            elif cmd == "/help":
                await self.tg.send_message(chat_id, texts.HELP)
            elif cmd == "/watch":
                await self._watch(chat_id, args)
            elif cmd == "/unwatch":
                await self._unwatch(chat_id, args)
            elif cmd == "/name":
                await self._name(chat_id, args)
            elif cmd == "/list":
                await self._list(chat_id)
            elif cmd in ("/listfull", "/list_full"):
                await self._list(chat_id, full=True)
            elif cmd == "/status":
                await self._status(chat_id, args)
            else:
                await self.tg.send_message(chat_id, texts.UNKNOWN)
        except Exception:
            log.exception("ошибка обработки команды %s", cmd)

    def _at_limit(self, chat_id, ip, port) -> bool:
        if chat_id in config.UNLIMITED_IDS:
            return False
        already = any(
            w["ip"] == ip and w["port"] == port
            for w in self.store.list_watches(chat_id)
        )
        return not already and self.store.count_watches(chat_id) >= config.MAX_WATCHES_PER_CHAT

    async def _watch(self, chat_id, args):
        if not args:
            await self.tg.send_message(chat_id, texts.BAD_IP)
            return
        # CIDR -> BGP-only подписка (port=0, без TCP)
        if "/" in args[0]:
            await self._watch_cidr(chat_id, args[0])
            return
        if not valid_ip(args[0]):
            await self.tg.send_message(chat_id, texts.BAD_IP)
            return
        ip = public_ip(args[0])
        if ip is None:
            await self.tg.send_message(chat_id, texts.PRIVATE_IP)
            return
        port = config.DEFAULT_PORT
        if len(args) > 1:
            port = parse_port(args[1])
            if port is None:
                await self.tg.send_message(chat_id, texts.BAD_PORT)
                return
        if self._at_limit(chat_id, ip, port):
            await self.tg.send_message(
                chat_id, texts.limit_reached(config.MAX_WATCHES_PER_CHAT)
            )
            return
        prefix, asn = await bgp.ip_to_prefix(self.http, ip)
        self.store.add_watch(chat_id, ip, port, prefix, asn)

        from . import probe
        tcp = "open" if await probe.tcp_probe(ip, port, config.PROBE_TIMEOUT) else "closed"
        vis = await bgp.prefix_visibility(self.http, prefix)
        seeing = vis.seeing if vis else None
        total = vis.total if vis else None
        # базовый state — только если строки ещё нет (monitor ведёт baseline сам);
        # перезапись чужого baseline = глушение алертов другим подписчикам.
        self.store.ensure_state(
            ip, port, prefix, asn, tcp, seeing, total, int(time.time())
        )
        await self.tg.send_message(
            chat_id, texts.watch_added(
                ip, port, prefix, asn, tcp, seeing, total,
                self.store.get_label(chat_id, ip, port),
            )
        )

    async def _watch_cidr(self, chat_id, arg):
        """BGP-only подписка на подсеть (port=0, TCP не проверяется)."""
        net = public_net(arg)
        if net is None:
            await self.tg.send_message(chat_id, texts.BAD_CIDR)
            return
        base, cidr = net
        if self._at_limit(chat_id, base, 0):
            await self.tg.send_message(
                chat_id, texts.limit_reached(config.MAX_WATCHES_PER_CHAT)
            )
            return
        self.store.add_watch(chat_id, base, 0, cidr, None)
        vis = await bgp.prefix_visibility(self.http, cidr)
        seeing = vis.seeing if vis else None
        total = vis.total if vis else None
        self.store.ensure_state(
            base, 0, cidr, None, None, seeing, total, int(time.time())
        )
        await self.tg.send_message(
            chat_id, texts.watch_added(
                base, 0, cidr, None, None, seeing, total,
                self.store.get_label(chat_id, base, 0),
            )
        )

    async def _name(self, chat_id, args):
        if len(args) < 2:
            await self.tg.send_message(chat_id, texts.BAD_NAME)
            return
        if "/" in args[0]:
            net = public_net(args[0])
            ip = net[0] if net else None
        else:
            ip = public_ip(args[0])
        if ip is None:
            await self.tg.send_message(chat_id, texts.BAD_IP)
            return
        label = " ".join(args[1:])
        if len(label) > 64:
            await self.tg.send_message(chat_id, texts.BAD_NAME)
            return
        label = None if label == "-" else label
        if not self.store.set_label(chat_id, ip, label):
            await self.tg.send_message(chat_id, "Сначала добавь этот IP/подсеть через /watch.")
            return
        message = f"Название: <b>{texts.esc(label)}</b>" if label else "Название убрано"
        await self.tg.send_message(chat_id, f"{message} · {texts.code(args[0])}")

    async def _unwatch(self, chat_id, args):
        if not args:
            await self.tg.send_message(chat_id, texts.BAD_IP)
            return
        arg = args[0]
        if "/" in arg:  # CIDR -> ключ = адрес сети
            net = public_net(arg)
            if net is None:
                await self.tg.send_message(chat_id, texts.BAD_CIDR)
                return
            key = net[0]
        elif valid_ip(arg):
            key = arg
        else:
            await self.tg.send_message(chat_id, texts.BAD_IP)
            return
        target = texts.code(arg)
        n = self.store.remove_watch(chat_id, key)
        if n:
            await self.tg.send_message(chat_id, f"🗑 Больше не слежу за {target}")
        else:
            await self.tg.send_message(chat_id, f"{target} не был в списке.")

    async def _list(self, chat_id, full=False):
        """full=False — сводка + только лежащие (с cap). full=True — все подписки."""
        rows = self.store.list_watches(chat_id)
        if not rows:
            await self.tg.send_message(chat_id, texts.EMPTY_LIST)
            return
        up = down = unknown = 0
        states = []   # (w, st) все
        problems = []  # (w, st) только не-up
        for w in rows:
            st = self.store.get_state(w["ip"], w["port"])
            states.append((w, st))
            cls = monitor.classify_watch(w, st)
            if cls == "up":
                up += 1
            else:
                problems.append((w, st))
                if cls == "down":
                    down += 1
                else:
                    unknown += 1

        header = texts.list_header(len(rows), up, down, unknown)
        if full:
            header += texts.list_full_caption()
            to_show, truncated = states, 0
        else:
            if not problems:
                await self.tg.send_message(chat_id, texts.list_all_ok(len(rows)))
                return
            header += texts.list_problems_caption(len(problems))
            truncated = max(0, len(problems) - config.LIST_MAX_ROWS)
            to_show = problems[: config.LIST_MAX_ROWS]

        # пагинация: шапка в первом сообщении, дальше строки
        chunk, size = [header], len(header)
        for w, st in to_show:
            row = texts.list_row(w, st)
            if size + len(row) + 1 > 3500:  # лимит Telegram 4096
                await self.tg.send_message(chat_id, "\n".join(chunk))
                chunk, size = [], 0
            chunk.append(row)
            size += len(row) + 1
        if truncated:
            chunk.append(texts.list_more(truncated))
        if chunk:
            await self.tg.send_message(chat_id, "\n".join(chunk))

    async def _status(self, chat_id, args):
        if not args:
            await self.tg.send_message(chat_id, texts.NEED_IP)
            return
        # CIDR -> BGP-only статус (без TCP)
        if "/" in args[0]:
            net = public_net(args[0])
            if net is None:
                await self.tg.send_message(chat_id, texts.BAD_CIDR)
                return
            base, cidr = net
            vis = await bgp.prefix_visibility(self.http, cidr)
            seeing = vis.seeing if vis else None
            total = vis.total if vis else None
            await self.tg.send_message(
                chat_id, texts.status_msg(
                    base, 0, cidr, None, None, seeing, total,
                    self.store.get_label(chat_id, base, 0),
                )
            )
            return
        if not valid_ip(args[0]):
            await self.tg.send_message(chat_id, texts.NEED_IP)
            return
        ip = public_ip(args[0])
        if ip is None:
            await self.tg.send_message(chat_id, texts.PRIVATE_IP)
            return
        port = config.DEFAULT_PORT
        if len(args) > 1:
            port = parse_port(args[1])
            if port is None:
                await self.tg.send_message(chat_id, texts.BAD_PORT)
                return
        prefix, asn = await bgp.ip_to_prefix(self.http, ip)
        from . import probe
        tcp = "open" if await probe.tcp_probe(ip, port, config.PROBE_TIMEOUT) else "closed"
        vis = await bgp.prefix_visibility(self.http, prefix)
        seeing = vis.seeing if vis else None
        total = vis.total if vis else None
        await self.tg.send_message(
            chat_id, texts.status_msg(
                ip, port, prefix, asn, tcp, seeing, total,
                self.store.get_label(chat_id, ip, port),
            )
        )

    # --- циклы ---
    async def poll_loop(self):
        while True:
            try:
                updates = await self.tg.get_updates(self._offset, timeout=50)
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message") or upd.get("edited_message")
                    if msg:
                        await self.handle(msg)
            except Exception:
                log.exception("ошибка polling")
                await asyncio.sleep(3)

    async def monitor_loop(self):
        while True:
            try:
                n = await monitor.run_cycle(self.store, self.http, self.notify)
                log.info("цикл мониторинга: %d событий", n)
            except Exception:
                log.exception("ошибка цикла мониторинга")
            await asyncio.sleep(config.POLL_INTERVAL)

    async def run(self):
        me = await self.tg.get_me()
        log.info("запущен как @%s", (me.get("result") or {}).get("username"))
        await self.tg.set_my_commands(COMMANDS)
        await asyncio.gather(self.poll_loop(), self.monitor_loop())

    async def close(self):
        await self.tg.close()
        await self.http.aclose()
        self.store.close()


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # httpx/httpcore логируют полный URL с токеном бота — глушим, чтобы токен
    # не утекал в journald.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    bot = Bot()
    try:
        await bot.run()
    finally:
        await bot.close()
