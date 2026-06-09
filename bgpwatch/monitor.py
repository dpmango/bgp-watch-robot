"""Логика мониторинга: классификация, вычисление событий, цикл проверки."""
import asyncio
import logging
import time

from . import bgp, config, probe


def classify_bgp(vis) -> str:
    """Visibility|None -> 'up' | 'down' | 'unknown'."""
    if vis is None:
        return "unknown"
    return "up" if vis.ratio >= config.BGP_UP_THRESHOLD else "down"


def _old_bgp_class(old) -> str:
    """Классификация BGP из сохранённого state."""
    if old is None or not old["bgp_total"]:
        return "unknown"
    ratio = (old["bgp_seeing"] or 0) / old["bgp_total"]
    return "up" if ratio >= config.BGP_UP_THRESHOLD else "down"


def compute_events(old, new_tcp: str, new_bgp: str):
    """Сравнить прошлый state с новым чтением. -> список событий.

    Событие = реальный переход. Первый запуск (old=None) и unknown событий не дают.
    """
    events = []
    old_tcp = old["tcp"] if old else None
    old_bgp = _old_bgp_class(old)

    # new_tcp=None → BGP-only подписка (порт 0), TCP не проверяется
    if new_tcp is not None and old_tcp is not None and new_tcp != old_tcp:
        events.append({"kind": "tcp", "from": old_tcp, "to": new_tcp})

    if new_bgp != "unknown" and old_bgp != "unknown" and new_bgp != old_bgp:
        events.append({"kind": "bgp", "from": old_bgp, "to": new_bgp})

    return events


async def run_cycle(store, http_client, notify):
    """Один проход мониторинга.

    notify: async callable (chat_id, ip, port, prefix, asn, tcp, seeing, total, events).
    -> число сгенерированных событий (для логов/тестов).
    """
    targets = store.distinct_targets()
    if not targets:
        return 0

    # 1) пробы параллельно — только для host-подписок (port != 0).
    #    port == 0 = BGP-only подписка (диапазон), TCP не проверяется.
    probe_targets = [(t["ip"], t["port"]) for t in targets if t["port"] != 0]
    probes = (
        await probe.probe_many(
            probe_targets, config.PROBE_CONCURRENCY, config.PROBE_TIMEOUT
        )
        if probe_targets
        else {}
    )

    # 2) BGP по уникальным префиксам, параллельно (семафор BGP_CONCURRENCY).
    #    Потолок на цикл — защита от DoS/исчерпания бюджета RIPEstat.
    prefixes = sorted({t["prefix"] for t in targets if t["prefix"]})
    if len(prefixes) > config.MAX_PREFIXES_PER_CYCLE:
        logging.getLogger("bgpwatch").warning(
            "префиксов %d > лимита %d за цикл; обрабатываю первые %d",
            len(prefixes), config.MAX_PREFIXES_PER_CYCLE, config.MAX_PREFIXES_PER_CYCLE,
        )
        prefixes = prefixes[: config.MAX_PREFIXES_PER_CYCLE]

    sem = asyncio.Semaphore(max(1, config.BGP_CONCURRENCY))

    async def _fetch(pfx):
        async with sem:
            return pfx, await bgp.prefix_visibility(http_client, pfx)

    vis_map = dict(await asyncio.gather(*(_fetch(p) for p in prefixes)))

    now = int(time.time())
    total_events = 0

    for t in targets:
        ip, port, pfx, asn = t["ip"], t["port"], t["prefix"], t["asn"]
        tcp = None if port == 0 else ("open" if probes.get((ip, port)) else "closed")
        vis = vis_map.get(pfx)
        new_bgp = classify_bgp(vis)

        old = store.get_state(ip, port)
        events = compute_events(old, tcp, new_bgp)

        # seeing/total: при unknown переносим старое значение
        if vis is not None:
            seeing, total = vis.seeing, vis.total
        elif old is not None:
            seeing, total = old["bgp_seeing"], old["bgp_total"]
        else:
            seeing, total = None, None

        store.upsert_state(
            ip, port, pfx, asn, tcp, seeing, total, bool(events), now
        )

        if events:
            total_events += len(events)
            for chat_id in store.chats_for(ip, port):
                await notify(chat_id, ip, port, pfx, asn, tcp, seeing, total, events)

    return total_events
