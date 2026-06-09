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
    {"command": "watch", "description": "Следить за IP: /watch 1.2.3.4 [порт]"},
    {"command": "unwatch", "description": "Перестать следить: /unwatch 1.2.3.4"},
    {"command": "list", "description": "Мои IP под наблюдением"},
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
            chat_id, texts.event_msg(ip, port, prefix, asn, tcp, seeing, total, events)
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
            elif cmd == "/list":
                await self._list(chat_id)
            elif cmd == "/status":
                await self._status(chat_id, args)
            else:
                await self.tg.send_message(chat_id, texts.UNKNOWN)
        except Exception:
            log.exception("ошибка обработки команды %s", cmd)

    async def _watch(self, chat_id, args):
        if not args or not valid_ip(args[0]):
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
        # лимит подписок на чат (не считаем повторное добавление того же target)
        already = any(
            w["ip"] == ip and w["port"] == port
            for w in self.store.list_watches(chat_id)
        )
        if not already and self.store.count_watches(chat_id) >= config.MAX_WATCHES_PER_CHAT:
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
        # базовый state, чтобы monitor не выстрелил повторно
        self.store.upsert_state(
            ip, port, prefix, asn, tcp, seeing, total, True, int(time.time())
        )
        await self.tg.send_message(
            chat_id, texts.watch_added(ip, port, prefix, asn, tcp, seeing, total)
        )

    async def _unwatch(self, chat_id, args):
        if not args or not valid_ip(args[0]):
            await self.tg.send_message(chat_id, texts.BAD_IP)
            return
        target = texts.esc(args[0])
        n = self.store.remove_watch(chat_id, args[0])
        if n:
            await self.tg.send_message(chat_id, f"🗑 Больше не слежу за <b>{target}</b>")
        else:
            await self.tg.send_message(chat_id, f"<b>{target}</b> не был в списке.")

    async def _list(self, chat_id):
        rows = self.store.list_watches(chat_id)
        if not rows:
            await self.tg.send_message(chat_id, texts.EMPTY_LIST)
            return
        header = f"<b>Мои IP под наблюдением ({len(rows)}):</b>"
        chunk, size = [header], len(header)
        for w in rows:
            st = self.store.get_state(w["ip"], w["port"])
            row = texts.list_row(w, st)
            if size + len(row) + 1 > 3500:  # лимит Telegram 4096
                await self.tg.send_message(chat_id, "\n".join(chunk))
                chunk, size = [], 0
            chunk.append(row)
            size += len(row) + 1
        if chunk:
            await self.tg.send_message(chat_id, "\n".join(chunk))

    async def _status(self, chat_id, args):
        if not args or not valid_ip(args[0]):
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
            chat_id, texts.status_msg(ip, port, prefix, asn, tcp, seeing, total)
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
