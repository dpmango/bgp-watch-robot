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
