import bgpwatch.config as cfg
from bgpwatch import bot


def test_public_ip_allows_global():
    assert bot.public_ip("8.8.8.8") == "8.8.8.8"
    assert bot.public_ip("1.1.1.1") == "1.1.1.1"
    assert bot.public_ip("2606:4700:4700::1111") == "2606:4700:4700::1111"


def test_public_ip_blocks_ssrf_targets():
    blocked = [
        "127.0.0.1",        # loopback
        "10.0.0.1",         # RFC1918
        "172.16.5.5",       # RFC1918
        "192.168.1.1",      # RFC1918
        "169.254.169.254",  # cloud metadata / link-local
        "0.0.0.0",          # unspecified
        "224.0.0.1",        # multicast
        "::1",              # IPv6 loopback
        "fe80::1",          # IPv6 link-local
        "fc00::1",          # IPv6 ULA
        "::ffff:127.0.0.1", # IPv4-mapped loopback
        "::ffff:10.0.0.1",  # IPv4-mapped RFC1918
        "not-an-ip",
    ]
    for ip in blocked:
        assert bot.public_ip(ip) is None, ip


def _make_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BOT_TOKEN", "test:token")
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "b.db"))
    return bot.Bot()


def test_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "RATE_LIMIT_CMDS", 3)
    monkeypatch.setattr(cfg, "RATE_LIMIT_WINDOW", 60.0)
    b = _make_bot(tmp_path, monkeypatch)
    assert b._rate_ok(42) is True
    assert b._rate_ok(42) is True
    assert b._rate_ok(42) is True
    assert b._rate_ok(42) is False          # 4-я в окне — отказ
    assert b._rate_ok(99) is True           # другой чат не задет
    b.store.close()


def test_rate_limit_evicts_stale_chats(tmp_path, monkeypatch):
    """dict _rl не растёт неограниченно: при превышении порога протухшие
    (без активности в окне) ключи выселяются."""
    monkeypatch.setattr(cfg, "RATE_LIMIT_CMDS", 5)
    monkeypatch.setattr(cfg, "RATE_LIMIT_WINDOW", 60.0)
    monkeypatch.setattr(cfg, "RATE_LIMIT_MAX_CHATS", 10)
    b = _make_bot(tmp_path, monkeypatch)
    clock = {"t": 0.0}
    monkeypatch.setattr(bot.time, "monotonic", lambda: clock["t"])

    for cid in range(10):                    # 10 чатов на t=0 — ровно порог
        assert b._rate_ok(cid)
    assert len(b._rl) == 10

    clock["t"] = 1000.0                       # перемотка за окно (60с)
    assert b._rate_ok(999)                    # 11-й ключ -> len>порог -> GC
    assert len(b._rl) == 1                    # 10 старых протухли, выселены
    assert 999 in b._rl
    b.store.close()


def test_unwatch_escapes_reflected_input(tmp_path, monkeypatch):
    """valid_ip() пропускает IPv6 scope_id с '<'/'>' (напр. 'IP%<img src=x>').
    _unwatch отражает ввод в HTML-ответ — он обязан экранироваться."""
    import asyncio

    b = _make_bot(tmp_path, monkeypatch)
    sent = []

    class FakeTg:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    b.tg = FakeTg()
    payload = "2606:4700:4700::1111%<img src=x>"
    assert bot.valid_ip(payload)  # подтверждаем, что ввод доходит до отражения
    asyncio.run(b._unwatch(7, [payload]))
    b.store.close()

    assert sent, "ожидали ответ"
    assert "<img" not in sent[-1]      # сырой тег не утёк
    assert "&lt;img" in sent[-1]       # экранирован
