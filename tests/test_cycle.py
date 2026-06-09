import asyncio

from bgpwatch import monitor
from bgpwatch.bgp import Visibility
from bgpwatch.store import Store


def test_run_cycle_fires_on_change(tmp_path, monkeypatch):
    db = Store(str(tmp_path / "c.db"))
    db.add_watch(111, "1.2.3.4", 22, "1.2.3.0/24", 216071)
    db.add_watch(222, "1.2.3.4", 22, "1.2.3.0/24", 216071)  # второй подписчик
    # базовый статус: всё ок
    db.upsert_state("1.2.3.4", 22, "1.2.3.0/24", 216071, "open", 326, 326, True, 1)

    async def fake_probe_many(targets, concurrency, timeout):
        return {(ip, port): False for ip, port in targets}  # упал

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

    async def fake_probe_many(targets, concurrency, timeout):
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
