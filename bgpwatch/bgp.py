"""RIPEstat-клиент: IP → анонсируемый префикс, префикс → BGP-видимость.

Парсеры (`parse_*`) — чистые функции, тестируются без сети.
Сетевые функции ретраят и возвращают None/fallback при сбое (RIPEstat иногда
роняет соединение — это норма, не повод для алерта).
"""
import asyncio
import ipaddress
from dataclasses import dataclass

from . import config

NETWORK_INFO = "https://stat.ripe.net/data/network-info/data.json"
ROUTING_STATUS = "https://stat.ripe.net/data/routing-status/data.json"


@dataclass(frozen=True)
class Visibility:
    seeing: int
    total: int

    @property
    def ratio(self) -> float:
        return self.seeing / self.total if self.total else 0.0

    @property
    def pct(self) -> int:
        return round(self.ratio * 100)


def parse_network_info(payload: dict):
    """-> (prefix|None, asn|None) из ответа network-info."""
    data = payload.get("data") or {}
    prefix = data.get("prefix")
    asns = data.get("asns") or []
    asn = None
    if asns:
        try:
            asn = int(asns[0])
        except (ValueError, TypeError):
            asn = None
    return prefix, asn


def parse_routing_status(payload: dict):
    """-> Visibility|None из ответа routing-status (IPv4-видимость)."""
    data = payload.get("data") or {}
    vis = (data.get("visibility") or {}).get("v4") or {}
    seeing = vis.get("ris_peers_seeing")
    total = vis.get("total_ris_peers")
    if seeing is None or total is None:
        return None
    try:
        return Visibility(int(seeing), int(total))
    except (ValueError, TypeError):
        return None


def fallback_prefix(ip: str) -> str:
    """Грубый /24 (IPv4) или /64 (IPv6) если RIPEstat не вернул префикс."""
    addr = ipaddress.ip_address(ip)
    bits = 24 if addr.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{bits}", strict=False))


async def _get_json(client, url: str, params: dict):
    """GET с ретраями. -> dict|None."""
    for attempt in range(config.RIPE_RETRIES):
        try:
            r = await client.get(url, params=params, timeout=config.RIPE_TIMEOUT)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        await asyncio.sleep(1.0 * (attempt + 1))
    return None


async def ip_to_prefix(client, ip: str):
    """-> (prefix, asn|None). При сбое RIPEstat — fallback /24."""
    params = {"resource": ip, "sourceapp": config.RIPE_SOURCEAPP}
    payload = await _get_json(client, NETWORK_INFO, params)
    if payload:
        prefix, asn = parse_network_info(payload)
        if prefix and _valid_prefix(prefix):  # не доверяем внешнему формату
            return prefix, asn
    return fallback_prefix(ip), None


def _valid_prefix(prefix: str) -> bool:
    try:
        ipaddress.ip_network(prefix, strict=False)
        return True
    except ValueError:
        return False


async def prefix_visibility(client, prefix: str):
    """-> Visibility|None (None = данных нет, не алертить)."""
    params = {"resource": prefix, "sourceapp": config.RIPE_SOURCEAPP}
    payload = await _get_json(client, ROUTING_STATUS, params)
    if payload:
        return parse_routing_status(payload)
    return None
