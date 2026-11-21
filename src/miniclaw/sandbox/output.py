import asyncio
import contextlib
from collections.abc import Callable

from miniclaw.sandbox.types import SandboxResourceExceeded


async def communicate_bounded(process, limit: int, timeout_seconds: float, *,
                              terminate: Callable[[], None] | None = None):
    total = 0

    async def consume(stream):
        nonlocal total
        output = bytearray()
        while chunk := await stream.read(65536):
            total += len(chunk)
            if total > limit:
                raise SandboxResourceExceeded('process output exceeded limit')
            output.extend(chunk)
        return bytes(output)

    readers = [asyncio.create_task(consume(process.stdout)),
               asyncio.create_task(consume(process.stderr))]
    try:
        async with asyncio.timeout(timeout_seconds):
            stdout, stderr = await asyncio.gather(*readers)
            await process.wait()
        return stdout, stderr
    except BaseException:
        if terminate is not None:
            terminate()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        raise
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
