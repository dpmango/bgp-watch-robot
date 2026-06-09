import asyncio

import bgpwatch.config as cfg
from bgpwatch import bot, monitor
from bgpwatch.bgp import Visibility
from bgpwatch.store import Store


def test_public_net_valid():
    assert bot.public_net("185.157.212.0/24") == ("185.157.212.0", "185.157.212.0/24")
    assert bot.public_net("8.8.8.128/25")[0] == "8.8.8.128"


def test_public_net_rejects():
    assert bot.public_net("10.0.0.0/8") is None        # приватная
    assert bot.public_net("127.0.0.0/8") is None        # loopback
    assert bot.public_net("8.8.8.8") is None            # не CIDR
    assert bot.public_net("garbage/24") is None


def test_compute_events_bgp_only():
    old = {"tcp": None, "bgp_seeing": 326, "bgp_total": 326}
    # BGP-only: tcp=None не генерит tcp-событий
    ev = monitor.compute_events(old, None, "down")
    assert ev == [{"kind": "bgp", "from": "up", "to": "down"}]
    assert monitor.compute_events(old, None, "up") == []


def test_run_cycle_bgp_only_skips_probe(tmp_path, monkeypatch):
    db = Store(str(tmp_path / "b.db"))
    db.add_watch(111, "185.157.212.0", 0, "185.157.212.0/24", None)  # port 0 = BGP-only
    db.upsert_state("185.157.212.0", 0, "185.157.212.0/24", None, None, 326, 326, True, 1)

    async def boom(*a, **k):
        raise AssertionError("probe_many не должен вызываться для BGP-only")

    async def fake_vis(client, prefix):
        return Visibility(1, 326)  # префикс ушёл

    monkeypatch.setattr(monitor.probe, "probe_many", boom)
    monkeypatch.setattr(monitor.bgp, "prefix_visibility", fake_vis)

    sent = []

    async def notify(chat_id, ip, port, prefix, asn, tcp, seeing, total, events):
        sent.append((tcp, [e["kind"] for e in events]))

    n = asyncio.run(monitor.run_cycle(db, None, notify))
    assert n == 1
    assert sent == [(None, ["bgp"])]  # tcp None, только bgp-событие
    db.close()


def _make_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BOT_TOKEN", "test:token")
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "b.db"))
    return bot.Bot()


def test_watch_cidr_creates_bgp_only(tmp_path, monkeypatch):
    b = _make_bot(tmp_path, monkeypatch)

    class FakeTg:
        sent = []
        async def send_message(self, chat_id, text, **kw):
            FakeTg.sent.append(text)

    b.tg = FakeTg()

    async def fake_vis(client, prefix):
        return Visibility(1, 326)

    monkeypatch.setattr(bot.bgp, "prefix_visibility", fake_vis)
    asyncio.run(b._watch(111, ["185.157.212.0/24"]))

    rows = b.store.list_watches(111)
    assert len(rows) == 1
    assert rows[0]["port"] == 0 and rows[0]["prefix"] == "185.157.212.0/24"
    assert "BGP-only" in FakeTg.sent[-1]
    b.store.close()


def test_classify_watch():
    # нет данных
    assert monitor.classify_watch({"port": 0}, None) == "unknown"
    # BGP-only
    assert monitor.classify_watch({"port": 0}, {"port": 0, "tcp": None, "bgp_seeing": 326, "bgp_total": 326}) == "up"
    assert monitor.classify_watch({"port": 0}, {"port": 0, "tcp": None, "bgp_seeing": 1, "bgp_total": 326}) == "down"
    # хост: down если TCP closed ИЛИ BGP down
    host_up = {"port": 22, "tcp": "open", "bgp_seeing": 326, "bgp_total": 326}
    host_tcp = {"port": 22, "tcp": "closed", "bgp_seeing": 326, "bgp_total": 326}
    host_bgp = {"port": 22, "tcp": "open", "bgp_seeing": 1, "bgp_total": 326}
    assert monitor.classify_watch({"port": 22}, host_up) == "up"
    assert monitor.classify_watch({"port": 22}, host_tcp) == "down"
    assert monitor.classify_watch({"port": 22}, host_bgp) == "down"


def test_list_summary_only_down(tmp_path, monkeypatch):
    b = _make_bot(tmp_path, monkeypatch)
    sent = []

    class FakeTg:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    b.tg = FakeTg()
    cid = 5
    # 1 up host, 1 down host, 1 down BGP-only подсеть
    b.store.add_watch(cid, "1.1.1.1", 22, "1.1.1.0/24", None)
    b.store.upsert_state("1.1.1.1", 22, "1.1.1.0/24", None, "open", 326, 326, True, 1)
    b.store.add_watch(cid, "2.2.2.2", 22, "2.2.2.0/24", None)
    b.store.upsert_state("2.2.2.2", 22, "2.2.2.0/24", None, "closed", 326, 326, True, 1)
    b.store.add_watch(cid, "3.3.3.0", 0, "3.3.3.0/24", None)
    b.store.upsert_state("3.3.3.0", 0, "3.3.3.0/24", None, None, 1, 326, True, 1)

    asyncio.run(b._list(cid))
    blob = "\n".join(sent)
    assert "Мои подписки: 3" in blob
    assert "up 1" in blob and "down 2" in blob
    assert "Показаны только лежащие (2)" in blob
    assert "2.2.2.2" in blob and "3.3.3.0/24" in blob
    assert "1.1.1.1" not in blob          # up не показываем
    # /listfull — показывает всё, включая up
    sent.clear()
    asyncio.run(b._list(cid, full=True))
    full = "\n".join(sent)
    assert "Все подписки" in full
    assert "1.1.1.1" in full and "2.2.2.2" in full and "3.3.3.0/24" in full
    b.store.close()


def test_watch_does_not_overwrite_monitor_baseline(tmp_path, monkeypatch):
    """Cross-tenant: чужой /watch не должен затирать baseline монитора и так
    глушить алерт для другого подписчика той же (ip,port)."""
    b = _make_bot(tmp_path, monkeypatch)

    class FakeTg:
        async def send_message(self, chat_id, text, **kw):
            pass

    b.tg = FakeTg()

    victim, attacker = 100, 200
    ip = "8.8.8.8"  # реально публичный (203.0.113.0/24 — documentation, отсеется)
    # подписка жертвы + baseline монитора = up/open
    b.store.add_watch(victim, ip, 22, "8.8.8.0/24", 65000)
    b.store.upsert_state(ip, 22, "8.8.8.0/24", 65000, "open", 326, 326, True, 1000)

    # цель упала; /watch атакующего пробит уже закрытый порт
    async def fake_prefix(client, ip_):
        return "8.8.8.0/24", 65000

    async def fake_vis(client, prefix):
        return Visibility(326, 326)

    async def fake_probe(ip_, port, timeout):
        return False  # closed

    monkeypatch.setattr(bot.bgp, "ip_to_prefix", fake_prefix)
    monkeypatch.setattr(bot.bgp, "prefix_visibility", fake_vis)
    from bgpwatch import probe as probe_mod
    monkeypatch.setattr(probe_mod, "tcp_probe", fake_probe)

    asyncio.run(b._watch(attacker, [ip]))

    # baseline обязан остаться "open" — closed-проба атакующего проигнорирована
    assert b.store.get_state(ip, 22)["tcp"] == "open"

    # цикл монитора теперь видит open->closed и уведомляет жертву
    sent = []

    async def notify(chat_id, *a):
        sent.append(chat_id)

    async def vis_up(client, prefix):
        return Visibility(326, 326)

    async def probe_many(targets, conc, timeout):
        return {(ip, 22): False}  # closed

    monkeypatch.setattr(monitor.bgp, "prefix_visibility", vis_up)
    monkeypatch.setattr(monitor.probe, "probe_many", probe_many)
    asyncio.run(monitor.run_cycle(b.store, b.http, notify))
    assert victim in sent
    b.store.close()


def test_unlimited_bypass(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MAX_WATCHES_PER_CHAT", 1)
    monkeypatch.setattr(cfg, "UNLIMITED_IDS", {777})
    monkeypatch.setattr(cfg, "RATE_LIMIT_CMDS", 1)
    b = _make_bot(tmp_path, monkeypatch)
    # rate-limit не действует на whitelisted
    assert b._rate_ok(777) and b._rate_ok(777)
    # лимит подписок не действует
    b.store.add_watch(777, "1.1.1.1", 22, "1.1.1.0/24", None)
    assert b._at_limit(777, "2.2.2.2", 22) is False
    # для обычного чата лимит работает
    b.store.add_watch(555, "1.1.1.1", 22, "1.1.1.0/24", None)
    assert b._at_limit(555, "3.3.3.3", 22) is True
    b.store.close()
