import asyncio

import pytest

from egressproxy.proxy import Allow, EgressProxy, ProxyConfig, _is_private

LOCAL = "127.0.0.1"


async def upstream(handler):  # type: ignore[no-untyped-def]
    server = await asyncio.start_server(handler, LOCAL, 0)
    return server, server.sockets[0].getsockname()[1]


async def echo_http(reader, writer):  # type: ignore[no-untyped-def]
    """Records the request head it receives and answers 200 with it as the body."""
    head = await reader.readuntil(b"\r\n\r\n")
    body = b""
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            body = await reader.readexactly(int(line.split(b":")[1]))
    payload = head + body
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(payload) + payload)
    await writer.drain()
    writer.close()


async def tcp_echo(reader, writer):  # type: ignore[no-untyped-def]
    while data := await reader.read(1024):
        writer.write(data.upper())
        await writer.drain()
    writer.close()


async def start_proxy(allow, **kw):  # type: ignore[no-untyped-def]
    proxy = EgressProxy(ProxyConfig(tuple(allow), **kw))
    server = await proxy.start(LOCAL, 0)
    return proxy, server, server.sockets[0].getsockname()[1]


async def talk(port: int, request: bytes, *, read: int = 65536, timeout: float = 5.0) -> bytes:
    reader, writer = await asyncio.open_connection(LOCAL, port)
    writer.write(request)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(read), timeout)
    finally:
        writer.close()


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_forwards_allowed_http_in_origin_form_and_strips_proxy_headers() -> None:
    async def main() -> None:
        up, up_port = await upstream(echo_http)
        proxy, server, port = await start_proxy([Allow("localhost", up_port, allow_private=True)])
        req = (f"POST http://localhost:{up_port}/v1/runs/x/events?q=1 HTTP/1.1\r\nHost: localhost:{up_port}\r\n"
               "Proxy-Authorization: Basic c2VjcmV0\r\nProxy-Connection: keep-alive\r\nConnection: keep-alive\r\n"
               "Authorization: Bearer run-token\r\nContent-Length: 5\r\n\r\nhello").encode()
        resp = await talk(port, req)
        assert resp.startswith(b"HTTP/1.1 200")
        seen = resp.split(b"\r\n\r\n", 1)[1]
        assert seen.startswith(b"POST /v1/runs/x/events?q=1 HTTP/1.1\r\n") and seen.endswith(b"hello")
        low = seen.lower()
        assert b"proxy-authorization" not in low and b"proxy-connection" not in low and b"keep-alive" not in low
        assert b"authorization: bearer run-token" in low and b"connection: close" in low      # end-to-end headers survive
        assert proxy.stats["allowed_http"] == 1
        server.close(); up.close()
    run(main())


def test_connect_tunnels_bytes_to_allowed_targets() -> None:
    async def main() -> None:
        up, up_port = await upstream(tcp_echo)
        proxy, server, port = await start_proxy([Allow("localhost", up_port, allow_private=True)])
        reader, writer = await asyncio.open_connection(LOCAL, port)
        writer.write(f"CONNECT localhost:{up_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"tls bytes pass through")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(100), 5) == b"TLS BYTES PASS THROUGH"
        writer.close(); server.close(); up.close()
        assert proxy.stats["allowed_connect"] == 1
    run(main())


@pytest.mark.parametrize(
    "request_line",
    ["CONNECT evil.example:443", "CONNECT localhost:1", "CONNECT 1.1.1.1:443", "CONNECT localhost", "CONNECT :443",
     "CONNECT [::1]:443", "CONNECT localhost:99999", "CONNECT localhost:0", "CONNECT localhost:abc",
     "GET http://evil.example/", "POST http://evil.example:8000/x", "GET http://1.2.3.4/", "GET http://localhost:1/"],
)
def test_everything_not_on_the_allowlist_is_denied(request_line: str) -> None:
    async def main() -> bytes:
        up, up_port = await upstream(tcp_echo)
        proxy, server, port = await start_proxy([Allow("localhost", up_port, allow_private=True)])
        resp = await talk(port, f"{request_line} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        server.close(); up.close()
        assert sum(v for k, v in proxy.stats.items() if k.startswith("denied")) == 1
        return resp
    assert run(main()).startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("method", ["DELETE", "PATCH", "TRACE", "OPTIONS", "HEAD"])
def test_only_get_post_put_and_connect_are_forwarded(method: str) -> None:
    async def main() -> bytes:
        up, up_port = await upstream(echo_http)
        proxy, server, port = await start_proxy([Allow("localhost", up_port, allow_private=True)])
        resp = await talk(port, f"{method} http://localhost:{up_port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        server.close(); up.close()
        assert proxy.stats["allowed_http"] == 0
        return resp
    assert run(main()).startswith(b"HTTP/1.1 405")


@pytest.mark.parametrize(
    "garbage",
    [b"\r\n\r\n", b"GET\r\n\r\n", b"GET / HTTP/1.1\r\n\r\n", b"GET https://x/ HTTP/1.1\r\n\r\n", b"GET ftp://x/ HTTP/1.1\r\n\r\n",
     b"CONNECT a:1 SPDY/3\r\n\r\n", b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"A" * 50 + b"\r\n\r\n"],
)
def test_malformed_or_non_proxy_requests_are_rejected(garbage: bytes) -> None:
    async def main() -> bytes:
        proxy, server, port = await start_proxy([Allow("localhost", 1)])
        resp = await talk(port, garbage)
        server.close()
        return resp
    assert run(main()).split(b" ")[1] in (b"400", b"403", b"405")


def test_a_non_private_entry_refuses_private_resolution() -> None:
    """The provider entry must never resolve to an internal address (SSRF / DNS rebinding)."""
    async def main() -> bytes:
        up, up_port = await upstream(tcp_echo)
        proxy, server, port = await start_proxy([Allow("localhost", up_port, allow_private=False)])
        resp = await talk(port, f"CONNECT localhost:{up_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        server.close(); up.close()
        assert proxy.stats["denied_private_address"] == 1
        return resp
    assert run(main()).startswith(b"HTTP/1.1 403")


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "::1", "fe80::1", "fc00::1", "0.0.0.0", "224.0.0.1"])
def test_private_address_classification(addr: str) -> None:
    assert _is_private(addr)


@pytest.mark.parametrize("addr", ["1.1.1.1", "8.8.8.8", "160.79.104.10", "2606:4700:4700::1111"])
def test_public_addresses_are_not_private(addr: str) -> None:
    assert not _is_private(addr)


def test_oversized_header_and_slow_clients_are_dropped() -> None:
    async def main() -> None:
        proxy, server, port = await start_proxy([Allow("localhost", 1)], head_timeout_s=0.3)
        huge = b"GET http://x/ HTTP/1.1\r\n" + b"X-A: " + b"a" * 70_000 + b"\r\n\r\n"
        resp = await talk(port, huge)
        assert resp.split(b" ")[1] in (b"400", b"431")
        reader, writer = await asyncio.open_connection(LOCAL, port)            # slowloris: never finishes its head
        writer.write(b"GET http://x/ HTTP/1.1\r\nHost:")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(10), 3) == b""               # closed by the proxy, no reply
        assert proxy.stats["head_timeout"] == 1
        writer.close(); server.close()
    run(main())


def test_connection_cap() -> None:
    async def main() -> None:
        up, up_port = await upstream(tcp_echo)
        proxy, server, port = await start_proxy([Allow("localhost", up_port, allow_private=True)], max_connections=2)
        held = []
        for _ in range(2):
            r, w = await asyncio.open_connection(LOCAL, port)
            w.write(f"CONNECT localhost:{up_port} HTTP/1.1\r\n\r\n".encode())
            await r.readuntil(b"\r\n\r\n")
            held.append(w)
        resp = await talk(port, f"CONNECT localhost:{up_port} HTTP/1.1\r\n\r\n".encode())
        assert resp.startswith(b"HTTP/1.1 503") and proxy.stats["refused_busy"] == 1
        for w in held:
            w.close()
        server.close(); up.close()
    run(main())


def test_upstream_failure_is_502_and_byte_cap_ends_the_tunnel() -> None:
    async def main() -> None:
        proxy, server, port = await start_proxy([Allow("localhost", 1, allow_private=True)])
        assert (await talk(port, b"CONNECT localhost:1 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 502")
        server.close()
        up, up_port = await upstream(tcp_echo)
        proxy, server, port = await start_proxy([Allow("localhost", up_port, allow_private=True)], max_bytes_per_connection=100)
        reader, writer = await asyncio.open_connection(LOCAL, port)
        writer.write(f"CONNECT localhost:{up_port} HTTP/1.1\r\n\r\n".encode())
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"x" * 500)
        await writer.drain()
        got = b""
        while chunk := await asyncio.wait_for(reader.read(1000), 5):
            got += chunk
        assert len(got) < 500 and proxy.stats["byte_cap"] >= 1
        server.close(); up.close()
    run(main())
