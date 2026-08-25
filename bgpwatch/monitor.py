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


def classify_watch(w, st) -> str:
    """Сводный статус подписки: 'up' | 'down' | 'unknown'.

    BGP-only (port=0) — по BGP. Хост — down если TCP closed ИЛИ BGP down.
    """
    if st is None:
        return "unknown"
    bgp = None
    if st["bgp_total"]:
        ratio = (st["bgp_seeing"] or 0) / st["bgp_total"]
        bgp = "up" if ratio >= config.BGP_UP_THRESHOLD else "down"
    if w["port"] == 0:  # BGP-only подписка
        return bgp or "unknown"
    tcp = st["tcp"]
    if tcp == "closed" or bgp == "down":
        return "down"
    if tcp == "open":
        return "up"
    return "unknown"


def _old_bgp_class(old) -> str:
    """Классификация BGP из сохранённого state."""
    if old is None or not old["bgp_total"]:
        return "unknown"
    ratio = (old["bgp_seeing"] or 0) / old["bgp_total"]
    return "up" if ratio >= config.BGP_UP_THRESHOLD else "down"


async def network_down(anchors, timeout: float) -> bool:
    """True если недоступны ВСЕ контрольные хосты = легла сеть мониторинга.

    Якоря стоят в независимых сетях, одновременный отказ всех означает проблему у
    нас. Без такой проверки моргнувший аплинк выглядит как падение чужих хостов.
    """
    if not anchors:
        return False
    res = await probe.probe_many(list(anchors), len(anchors), timeout)
    return not any(res.values())


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
            probe_targets,
            config.PROBE_CONCURRENCY,
            config.PROBE_TIMEOUT,
            config.PROBE_ATTEMPTS,
            config.PROBE_RETRY_DELAY,
        )
        if probe_targets
        else {}
    )

    # Прошлый статус нужен и для гейта ниже, и для diff'а — читаем один раз.
    olds = {(t["ip"], t["port"]): store.get_state(t["ip"], t["port"]) for t in targets}

    # Есть провалы — сверяемся с контрольными хостами. Легла наша сеть, а не чужие
    # хосты: TCP в этом цикле = "нет данных" (как unknown у BGP), держим прошлый
    # статус и не алертим. Проверка самозавершающаяся: как только связь есть,
    # вердикты возобновляются и реальное падение доедет до подписчика.
    blackout = False
    if not all(probes.values()):
        blackout = await network_down(config.ANCHORS, config.PROBE_TIMEOUT)
        if blackout:
            logging.getLogger("bgpwatch").warning(
                "контрольные хосты недоступны — сеть мониторинга легла, "
                "TCP-события этого цикла подавлены"
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
        if port == 0 or blackout:
            tcp = None  # BGP-only подписка либо сбой нашей сети — вердикта нет
        else:
            tcp = "open" if probes.get((ip, port)) else "closed"
        vis = vis_map.get(pfx)
        new_bgp = classify_bgp(vis)

        old = olds[(ip, port)]
        events = compute_events(old, tcp, new_bgp)

        # tcp=None при blackout не должен затирать прошлый статус: иначе следующий
        # цикл увидит old_tcp=None и промолчит уже про реальный переход.
        stored_tcp = tcp if tcp is not None else (old["tcp"] if old is not None else None)

        # seeing/total: при unknown переносим старое значение
        if vis is not None:
            seeing, total = vis.seeing, vis.total
        elif old is not None:
            seeing, total = old["bgp_seeing"], old["bgp_total"]
        else:
            seeing, total = None, None

        store.upsert_state(
            ip, port, pfx, asn, stored_tcp, seeing, total, bool(events), now
        )

        if events:
            total_events += len(events)
            for chat_id in store.chats_for(ip, port):
                await notify(
                    chat_id, ip, port, pfx, asn, stored_tcp, seeing, total, events
                )

    return total_events
