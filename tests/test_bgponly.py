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
