"""Асинхронная TCP-проба: принимает ли хост соединение на порт."""
import asyncio


async def tcp_probe(ip: str, port: int, timeout: float) -> bool:
    """True если TCP-соединение установилось за timeout, иначе False."""
    writer = None
    try:
        fut = asyncio.open_connection(ip, port)
        _reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        return True
    except Exception:
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def probe_many(targets, concurrency: int, timeout: float) -> dict:
    """targets: iterable (ip, port). -> {(ip,port): bool}."""
    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict = {}

    async def one(ip, port):
        async with sem:
            results[(ip, port)] = await tcp_probe(ip, port, timeout)

    await asyncio.gather(*(one(ip, port) for ip, port in targets))
    return results
