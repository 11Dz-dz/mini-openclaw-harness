import asyncio

import httpx
import pytest

from miniclaw.web.fetch import WebFetcher
from miniclaw.web.guard import UnsafeUrl, UrlGuard


async def test_dns_answer_is_used_as_destination_with_original_host_and_tls_name():
    answers = iter([('93.184.216.34',), ('127.0.0.1',)])
    seen = []

    def resolver(host, port):
        return next(answers)

    async def handler(request):
        seen.append(request)
        assert request.url.host == '93.184.216.34'
        assert request.headers['host'] == 'rebind.invalid:8443'
        assert request.extensions['sni_hostname'] == 'rebind.invalid'
        return httpx.Response(200, text='safe')

    fetcher = WebFetcher(guard=UrlGuard(resolver=resolver),
                         transport=httpx.MockTransport(handler))
    try:
        await fetcher.fetch('https://rebind.invalid:8443/page')
        with pytest.raises(UnsafeUrl):
            await fetcher.fetch('https://rebind.invalid:8443/page')
    finally:
        await fetcher.aclose()
    assert len(seen) == 1


async def test_real_transport_connects_to_pinned_ip_without_resolving_hostname_again():
    requests = []

    async def serve(reader, writer):
        requests.append(await reader.readuntil(b'\r\n\r\n'))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    lookups = []

    def resolver(host, port):
        lookups.append(host)
        return ('127.0.0.1',)  # private addresses allowed only for this local fixture

    fetcher = WebFetcher(guard=UrlGuard(resolver=resolver, allow_private=True))
    try:
        page = await fetcher.fetch(f'http://no-such-dns-name.invalid:{port}/')
        assert page.text == 'ok'
    finally:
        await fetcher.aclose()
        server.close()
        await server.wait_closed()
    assert lookups == ['no-such-dns-name.invalid']
    assert f'Host: no-such-dns-name.invalid:{port}'.encode() in requests[0]


@pytest.mark.parametrize('addresses', [(), ('100.64.0.1',), ('not-an-ip',)])
def test_empty_shared_and_invalid_addresses_fail_closed(addresses):
    with pytest.raises(UnsafeUrl):
        UrlGuard(resolver=lambda host, port: addresses).check('https://bad.invalid/')
