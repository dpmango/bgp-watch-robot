import asyncio

import bgpwatch.config as cfg
from bgpwatch import monitor
from bgpwatch.bgp import Visibility
from bgpwatch.store import Store


def test_run_cycle_fires_on_change(tmp_path, monkeypatch):
    db = Store(str(tmp_path / "c.db"))
    db.add_watch(111, "1.2.3.4", 22, "1.2.3.0/24", 216071)
    db.add_watch(222, "1.2.3.4", 22, "1.2.3.0/24", 216071)  # второй подписчик
    # базовый статус: всё ок
    db.upsert_state("1.2.3.4", 22, "1.2.3.0/24", 216071, "open", 326, 326, True, 1)

    # хост не отвечает, якоря отвечают -> сеть жива, это настоящее падение
    async def fake_probe_many(targets, concurrency, timeout, *a):
        return {t: t in cfg.ANCHORS for t in targets}

    async def fake_vis(client, prefix):
        return Visibility(1, 326)  # префикс ушёл

    monkeypatch.setattr(monitor.probe, "probe_many", fake_probe_many)
    monkeypatch.setattr(monitor.bgp, "prefix_visibility", fake_vis)

    sent = []

    async def notify(chat_id, ip, port, prefix, asn, tcp, seeing, total, events):
        sent.append((chat_id, tcp, [e["kind"] for e in events]))

    n = asyncio.run(monitor.run_cycle(db, None, notify))

    # 1 target, 2 события (tcp + bgp); оба подписчика уведомлены
    assert n == 2
    assert len(sent) == 2
    chat_ids = sorted(c for c, _, _ in sent)
    assert chat_ids == [111, 222]
    for _, tcp, kinds in sent:
        assert tcp == "closed"
        assert set(kinds) == {"tcp", "bgp"}
    db.close()


def test_run_cycle_silent_when_stable(tmp_path, monkeypatch):
    db = Store(str(tmp_path / "c2.db"))
    db.add_watch(111, "1.2.3.4", 22, "1.2.3.0/24", 216071)
    db.upsert_state("1.2.3.4", 22, "1.2.3.0/24", 216071, "open", 326, 326, True, 1)

    async def fake_probe_many(targets, concurrency, timeout, *a):
        return {(ip, port): True for ip, port in targets}

    async def fake_vis(client, prefix):
        return Visibility(326, 326)

    monkeypatch.setattr(monitor.probe, "probe_many", fake_probe_many)
    monkeypatch.setattr(monitor.bgp, "prefix_visibility", fake_vis)

    sent = []

    async def notify(*a):
        sent.append(a)

    n = asyncio.run(monitor.run_cycle(db, None, notify))
    assert n == 0 and sent == []
    db.close()


def test_blackout_suppresses_mass_false_alert(tmp_path, monkeypatch):
    """Легла наша сеть (контрольные хосты молчат) — это не падение чужих хостов.

    Регрессия: 12 хостов разных провайдеров «упали» и «вернулись» одним циклем.
    """
    db = Store(str(tmp_path / "bo.db"))
    hosts = [("1.2.3.4", "1.2.3.0/24"), ("5.6.7.8", "5.6.7.0/24"), ("9.10.11.12", "9.10.11.0/24")]
    for ip, pfx in hosts:
        db.add_watch(111, ip, 22, pfx, None)
        db.upsert_state(ip, 22, pfx, None, "open", 326, 326, True, 1)

    # не отвечает никто, включая якоря -> легла наша сеть
    async def all_fail(targets, concurrency, timeout, *a):
        return {t: False for t in targets}

    async def vis_up(client, prefix):
        return Visibility(326, 326)

    monkeypatch.setattr(monitor.probe, "probe_many", all_fail)
    monkeypatch.setattr(monitor.bgp, "prefix_visibility", vis_up)

    sent = []

    async def notify(*a):
        sent.append(a)

    n = asyncio.run(monitor.run_cycle(db, None, notify))
    assert n == 0 and sent == []
    # прошлый статус сохранён: иначе следующий цикл промолчит о реальном переходе
    for ip, _ in hosts:
        assert db.get_state(ip, 22)["tcp"] == "open"
    db.close()


def test_single_host_failure_still_alerts(tmp_path, monkeypatch):
    """Падение одного хоста при живых остальных — настоящий алерт, не глушим."""
    db = Store(str(tmp_path / "one.db"))
    hosts = [("1.2.3.4", "1.2.3.0/24"), ("5.6.7.8", "5.6.7.0/24"), ("9.10.11.12", "9.10.11.0/24")]
    for ip, pfx in hosts:
        db.add_watch(111, ip, 22, pfx, None)
        db.upsert_state(ip, 22, pfx, None, "open", 326, 326, True, 1)

    # якоря отвечают -> сеть жива, вердикт по хосту принимается
    async def one_fails(targets, concurrency, timeout, *a):
        return {(ip, port): ip != "1.2.3.4" for ip, port in targets}

    async def vis_up(client, prefix):
        return Visibility(326, 326)

    monkeypatch.setattr(monitor.probe, "probe_many", one_fails)
    monkeypatch.setattr(monitor.bgp, "prefix_visibility", vis_up)

    sent = []

    async def notify(chat_id, ip, port, prefix, asn, tcp, seeing, total, events):
        sent.append((ip, tcp, [e["kind"] for e in events]))

    n = asyncio.run(monitor.run_cycle(db, None, notify))
    assert n == 1
    assert sent == [("1.2.3.4", "closed", ["tcp"])]
    db.close()


def test_network_down_only_when_all_anchors_fail():
    """Один недоступный якорь — не повод глушить алерты."""
    async def scenario(anchor_results):
        async def fake(targets, conc, timeout, *a):
            return {t: anchor_results[i] for i, t in enumerate(targets)}

        import bgpwatch.probe as probe_mod
        real = probe_mod.probe_many
        probe_mod.probe_many = fake
        try:
            return await monitor.network_down([("a", 1), ("b", 2), ("c", 3)], 1.0)
        finally:
            probe_mod.probe_many = real

    assert asyncio.run(scenario([False, False, False])) is True
    assert asyncio.run(scenario([False, True, False])) is False
