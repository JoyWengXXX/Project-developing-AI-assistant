"""固定目的地的 TCP 橋接，不解析或記錄查詢。"""

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)
_CONNECTIONS = asyncio.Semaphore(32)


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    if _CONNECTIONS.locked():
        writer.close()
        await writer.wait_closed()
        return
    async with _CONNECTIONS:
        upstream: asyncio.StreamWriter | None = None
        try:
            target, connected = await asyncio.wait_for(
                asyncio.open_connection("embedding-gateway", 8765), timeout=5
            )
            upstream = connected
            tasks = {
                asyncio.create_task(_copy(reader, connected)),
                asyncio.create_task(_copy(target, writer)),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            outcomes = await asyncio.gather(*done, *pending, return_exceptions=True)
            if any(
                isinstance(outcome, BaseException)
                and not isinstance(outcome, asyncio.CancelledError)
                for outcome in outcomes
            ):
                logger.warning("embedding_gateway_proxy_connection_failed")
        except (OSError, TimeoutError):
            logger.warning("embedding_gateway_proxy_connection_failed")
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
                if upstream is not None:
                    await upstream.wait_closed()


async def _serve() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", 8765)  # noqa: S104
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
