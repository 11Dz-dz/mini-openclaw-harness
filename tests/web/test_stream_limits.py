import httpx
import pytest

from miniclaw.web.fetch import FetchError, WebFetcher
from miniclaw.web.guard import UrlGuard


async def test_fetch_stops_reading_and_closes_body_as_soon_as_limit_is_reached():
    class Body(httpx.AsyncByteStream):
        consumed = 0
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                self.consumed += 1
                yield b'x' * 128

        async def aclose(self):
            self.closed = True

    body = Body()
    fetcher = WebFetcher(max_bytes=1024,
        guard=UrlGuard(resolver=lambda host, port: ('93.184.216.34',)),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body)))
    try:
        page = await fetcher.fetch('https://example.com')
    finally:
        await fetcher.aclose()
    assert page.truncated
    assert len(page.text) == 1024
    assert body.consumed < 10
    assert body.closed


async def test_compressed_body_is_rejected_before_decoding():
    fetcher = WebFetcher(
        guard=UrlGuard(resolver=lambda host, port: ('93.184.216.34',)),
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={'content-encoding': 'gzip'}, stream=httpx.ByteStream(b'unsafe'))))
    try:
        with pytest.raises(FetchError, match='compressed'):
            await fetcher.fetch('https://example.com')
    finally:
        await fetcher.aclose()
