"""Минимальный async-клиент Telegram Bot API над httpx (long-polling).

Самолечение под РФ-блокировкой: api.telegram.org на этом хостинге доступен лишь
по части IP. Клиент коннектится по явному IP с SNI=api.telegram.org (TLS-серт
проверяется корректно). Если все известные IP отвалились — сканирует диапазоны,
находит новый рабочий bot-API хост, валидирует getMe, переключается и запоминает.
"""
import asyncio
import ipaddress
import logging
import os
import time

import httpx

log = logging.getLogger("bgpwatch.telegram")

HOST = "api.telegram.org"
_NET_ERRORS = (httpx.TransportError,)  # connect/timeout/read/network/protocol
_DISCOVER_COOLDOWN = 120.0  # не сканировать чаще раза в N секунд


class Telegram:
    def __init__(self, token, ips=None, discover_ranges=None, state_path=None):
        self._token = token
        self._ips = list(ips) if ips else []
        self._ranges = list(discover_ranges) if discover_ranges else []
        self._state_path = state_path
        self._idx = 0
        self._last_discover = 0.0
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(65.0))
        self._load_state()

    # --- персист рабочего IP ---
    def _load_state(self):
        if self._state_path and os.path.exists(self._state_path):
            try:
                ip = open(self._state_path).read().strip()
                if ip:
                    if ip in self._ips:
                        self._idx = self._ips.index(ip)
                    else:
                        self._ips.insert(0, ip)
            except Exception:
                pass

    def _save_state(self, ip):
        if not self._state_path:
            return
        try:
            with open(self._state_path, "w") as f:
                f.write(ip)
        except Exception:
            pass

    # --- запрос с failover ---
    def _url(self, ip, method):
        if ip:
            return (
                f"https://{ip}/bot{self._token}/{method}",
                {"Host": HOST},
                {"sni_hostname": HOST},
            )
        return (f"https://{HOST}/bot{self._token}/{method}", {}, {})

    async def _do(self, http_method, ip, method, *, params=None, json=None, timeout=None):
        url, headers, ext = self._url(ip, method)
        if http_method == "GET":
            return await self.client.get(
                url, params=params, headers=headers, extensions=ext, timeout=timeout
            )
        return await self.client.post(
            url, json=json, headers=headers, extensions=ext, timeout=timeout
        )

    async def _request(self, http_method, method, **req):
        candidates = self._ips or [None]
        # начинаем с текущего рабочего
        order = candidates[self._idx:] + candidates[: self._idx]
        last = None
        for ip in order:
            try:
                r = await self._do(http_method, ip, method, **req)
                if ip in self._ips:
                    self._idx = self._ips.index(ip)
                return r
            except _NET_ERRORS as e:
                last = e
                log.warning("Telegram IP %s недоступен: %s", ip, type(e).__name__)
        # все кандидаты мертвы — попытка авто-поиска (с кулдауном)
        found = await self._discover()
        if found:
            self._ips = [found] + [x for x in self._ips if x != found]
            self._idx = 0
            self._save_state(found)
            try:
                return await self._do(http_method, found, method, **req)
            except _NET_ERRORS as e:
                last = e
        if last:
            raise last
        raise httpx.ConnectError("no telegram endpoint")

    async def _discover(self):
        now = time.monotonic()
        if now - self._last_discover < _DISCOVER_COOLDOWN:
            return None
        self._last_discover = now
        ips = []
        for cidr in self._ranges:
            try:
                ips += [str(h) for h in ipaddress.ip_network(cidr, strict=False).hosts()]
            except ValueError:
                pass
        if not ips:
            return None
        log.warning("сканирую %d адресов на рабочий Telegram bot-API...", len(ips))
        sem = asyncio.Semaphore(200)

        async def open443(ip):
            async with sem:
                try:
                    fut = asyncio.open_connection(ip, 443)
                    _r, w = await asyncio.wait_for(fut, timeout=3)
                    w.close()
                    return ip
                except Exception:
                    return None

        reachable = [r for r in await asyncio.gather(*(open443(i) for i in ips)) if r]
        log.warning("443-открытых: %d, валидирую getMe...", len(reachable))
        for ip in reachable:
            try:
                r = await self._do("GET", ip, "getMe", timeout=8)
                if r.status_code == 200 and r.json().get("ok"):
                    log.warning("найден рабочий Telegram IP: %s", ip)
                    return ip
            except Exception:
                continue
        log.error("рабочий Telegram IP не найден")
        return None

    # --- публичный API ---
    async def get_me(self):
        r = await self._request("GET", "getMe", timeout=15)
        return r.json()

    async def get_updates(self, offset: int, timeout: int = 50):
        r = await self._request(
            "GET", "getUpdates",
            params={"offset": offset, "timeout": timeout}, timeout=timeout + 15,
        )
        data = r.json()
        return data.get("result", []) if data.get("ok") else []

    async def send_message(self, chat_id, text, parse_mode="HTML"):
        try:
            await self._request(
                "POST", "sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )
        except Exception:
            pass  # доставка best-effort

    async def set_my_commands(self, commands):
        try:
            await self._request(
                "POST", "setMyCommands", json={"commands": commands}, timeout=15
            )
        except Exception:
            pass

    async def close(self):
        await self.client.aclose()
