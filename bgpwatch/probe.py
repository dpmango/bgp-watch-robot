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


async def probe_many(
    targets, concurrency: int, timeout: float, attempts: int = 1, retry_delay: float = 0.0
) -> dict:
    """targets: iterable (ip, port). -> {(ip,port): bool}.

    attempts > 1: провалившиеся цели перепроверяются ещё раз через retry_delay.
    Одиночный потерянный SYN на нашем аплинке не должен выглядеть как падение хоста;
    успешные цели не перепроверяются, поэтому в норме overhead нулевой.
    """
    results: dict = {}
    pending = list(targets)

    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(retry_delay)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(ip, port):
            async with sem:
                results[(ip, port)] = await tcp_probe(ip, port, timeout)

        await asyncio.gather(*(one(ip, port) for ip, port in pending))
        pending = [t for t in pending if not results[t]]
        if not pending:
            break

    return results
