import asyncio

from bgpwatch import probe


def test_probe_open_and_closed():
    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            assert await probe.tcp_probe("127.0.0.1", port, 2.0) is True
        finally:
            server.close()
            await server.wait_closed()
        # порт закрыт после остановки сервера
        assert await probe.tcp_probe("127.0.0.1", port, 1.0) is False

    asyncio.run(scenario())


def test_probe_many():
    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            res = await probe.probe_many(
                [("127.0.0.1", port), ("127.0.0.1", 1)], concurrency=10, timeout=1.0
            )
        finally:
            server.close()
            await server.wait_closed()
        assert res[("127.0.0.1", port)] is True
        assert res[("127.0.0.1", 1)] is False

    asyncio.run(scenario())


def test_probe_retries_transient_failure():
    """Провалившаяся цель перепроверяется: короткий сбой сети != падение хоста."""
    calls = []

    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        real = probe.tcp_probe

        async def flaky(ip, p, timeout):
            calls.append((ip, p))
            return False if len(calls) == 1 else await real(ip, p, timeout)

        probe.tcp_probe = flaky
        try:
            res = await probe.probe_many(
                [("127.0.0.1", port)], concurrency=10, timeout=1.0,
                attempts=3, retry_delay=0.0,
            )
        finally:
            probe.tcp_probe = real
            server.close()
            await server.wait_closed()
        assert res[("127.0.0.1", port)] is True
        assert len(calls) == 2  # вторая попытка успешна — третьей нет

    asyncio.run(scenario())


def test_probe_no_retry_when_all_open():
    """Успешные цели не перепроверяются — в норме ретрай ничего не стоит."""
    calls = []

    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        real = probe.tcp_probe

        async def counted(ip, p, timeout):
            calls.append((ip, p))
            return await real(ip, p, timeout)

        probe.tcp_probe = counted
        try:
            await probe.probe_many(
                [("127.0.0.1", port)], concurrency=10, timeout=1.0,
                attempts=3, retry_delay=0.0,
            )
        finally:
            probe.tcp_probe = real
            server.close()
            await server.wait_closed()
        assert len(calls) == 1

    asyncio.run(scenario())
