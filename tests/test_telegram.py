import asyncio

import httpx

from bgpwatch.telegram import Telegram


class FakeResp:
    def __init__(self, data):
        self._d = data
        self.status_code = 200

    def json(self):
        return self._d


def test_url_pinning():
    tg = Telegram("tok", ips=["1.1.1.1"], state_path=None)
    url, headers, ext = tg._url("1.1.1.1", "getMe")
    assert url == "https://1.1.1.1/bottok/getMe"
    assert headers["Host"] == "api.telegram.org"
    assert ext["sni_hostname"] == "api.telegram.org"


def test_url_dns_fallback():
    tg = Telegram("tok", ips=[], state_path=None)
    url, headers, ext = tg._url(None, "getMe")
    assert url == "https://api.telegram.org/bottok/getMe"
    assert headers == {} and ext == {}


def test_failover_to_next_ip(monkeypatch):
    tg = Telegram("tok", ips=["10.0.0.1", "10.0.0.2"], state_path=None)
    calls = []

    async def fake_do(http_method, ip, method, **kw):
        calls.append(ip)
        if ip == "10.0.0.1":
            raise httpx.ConnectError("down")
        return FakeResp({"ok": True, "result": []})

    monkeypatch.setattr(tg, "_do", fake_do)
    out = asyncio.run(tg.get_updates(0))
    assert out == []
    assert calls == ["10.0.0.1", "10.0.0.2"]
    assert tg._idx == 1  # запомнил рабочий


def test_failover_triggers_discovery(monkeypatch):
    tg = Telegram("tok", ips=["10.0.0.1"], state_path=None)

    async def fake_do(http_method, ip, method, **kw):
        if ip == "9.9.9.9":
            return FakeResp({"ok": True, "result": []})
        raise httpx.ConnectError("down")

    async def fake_discover():
        return "9.9.9.9"

    monkeypatch.setattr(tg, "_do", fake_do)
    monkeypatch.setattr(tg, "_discover", fake_discover)
    out = asyncio.run(tg.get_updates(0))
    assert out == []
    assert tg._ips[0] == "9.9.9.9" and tg._idx == 0
