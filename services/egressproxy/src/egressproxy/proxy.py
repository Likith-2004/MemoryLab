"""Allowlist egress proxy for run sandboxes.

Sandboxes sit on an internal network with no route out; this proxy is their only way to reach
the model provider (CONNECT, TLS passes through untouched) and our ingest service (plain HTTP
forwarding). Everything else is refused. Properties:

- exact host:port allowlist, no wildcards, no redirects, no other methods or schemes;
- names are resolved here and the connection goes to the vetted address (no rebinding), and a
  name that resolves to a private/loopback/link-local address is refused unless the allowlist
  entry is explicitly marked `allow_private` (ingest lives on a private network, the provider
  does not);
- request heads, idle time, connection counts and bytes per connection are all bounded.
"""

import asyncio
import ipaddress
import logging
import socket
from collections import Counter
from dataclasses import dataclass

log = logging.getLogger("egressproxy")

MAX_HEAD_BYTES = 16 * 1024
# Hop-by-hop headers are dropped; Content-Length and Transfer-Encoding are kept so the body stays framed.
HOP_BY_HOP = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"proxy-connection",
              b"te", b"trailer", b"upgrade"}


@dataclass(frozen=True)
class Allow:
    host: str
    port: int
    allow_private: bool = False


@dataclass(frozen=True)
class ProxyConfig:
    allowlist: tuple[Allow, ...]
    max_connections: int = 64
    head_timeout_s: float = 10.0
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 60.0
    max_bytes_per_connection: int = 256 * 1024 * 1024


def _is_private(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%")[0])
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified)


class EgressProxy:
    def __init__(self, config: ProxyConfig) -> None:
        self.config = config
        self._allowed = {(a.host.lower(), a.port): a for a in config.allowlist}
        self._active = 0
        self.stats: Counter[str] = Counter()

    # ---- server ---------------------------------------------------------------------------
    async def start(self, host: str = "0.0.0.0", port: int = 3128) -> asyncio.Server:
        return await asyncio.start_server(self._handle, host, port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= self.config.max_connections:
            self.stats["refused_busy"] += 1
            await self._reply(writer, 503, "busy")
            return
        self._active += 1
        try:
            await self._serve(reader, writer)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, OSError):
            self.stats["connection_errors"] += 1
        finally:
            self._active -= 1
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.config.head_timeout_s)
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            await self._reply(writer, 400, "bad request")
            return
        except TimeoutError:
            self.stats["head_timeout"] += 1
            return
        if len(head) > MAX_HEAD_BYTES:
            await self._reply(writer, 431, "headers too large")
            return
        lines = head[:-4].split(b"\r\n")
        parts = lines[0].split(b" ")
        if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
            await self._reply(writer, 400, "bad request")
            return
        method, target = parts[0].upper(), parts[1]
        if method == b"CONNECT":
            await self._connect(reader, writer, target)
        elif method not in (b"GET", b"POST", b"PUT"):
            self.stats["denied_method"] += 1
            await self._reply(writer, 405, "method not allowed")
        elif target.lower().startswith(b"http://"):
            await self._forward(reader, writer, method, target, lines[1:])
        else:
            self.stats["denied_scheme"] += 1
            await self._reply(writer, 400, "only http:// absolute URIs and CONNECT are supported")

    # ---- policy -----------------------------------------------------------------------------
    async def _vet(self, host: str, port: int) -> list[str] | None:
        """Return the vetted addresses to try if host:port is allowed, else None. Names are resolved
        here and only these addresses are used (no second lookup, so no DNS rebinding)."""
        entry = self._allowed.get((host.lower(), port))
        if entry is None:
            self.stats["denied_not_allowlisted"] += 1
            log.info("denied host=%s port=%s reason=not_allowlisted", host[:80], port)
            return None
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            self.stats["denied_unresolvable"] += 1
            return None
        usable = [str(info[4][0]) for info in infos if entry.allow_private or not _is_private(str(info[4][0]))]
        if not usable:
            self.stats["denied_private_address"] += 1
            log.info("denied host=%s port=%s reason=private_address", host[:80], port)
            return None
        return list(dict.fromkeys(usable))

    async def _dial(self, addresses: list[str], port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        for addr in addresses:
            try:
                return await asyncio.wait_for(asyncio.open_connection(addr, port), self.config.connect_timeout_s)
            except (TimeoutError, OSError):
                continue
        self.stats["upstream_failed"] += 1
        return None

    @staticmethod
    def _split(target: bytes, default_port: int | None) -> tuple[str, int] | None:
        text = target.decode("ascii", "ignore")
        if text.startswith("["):  # IPv6 literal
            host, _, rest = text[1:].partition("]")
            port_text = rest.removeprefix(":")
        elif ":" in text:
            host, _, port_text = text.rpartition(":")
        else:
            host, port_text = text, ""
        port = int(port_text) if port_text.isdigit() else default_port
        if not host or port is None or not 0 < port < 65536:
            return None
        return host, port

    # ---- CONNECT --------------------------------------------------------------------------------
    async def _connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: bytes) -> None:
        split = self._split(target, None)
        vetted = await self._vet(*split) if split else None
        if split is None:
            self.stats["denied_malformed"] += 1
        if vetted is None or split is None:
            await self._reply(writer, 403, "egress denied")
            return
        dialled = await self._dial(vetted, split[1])
        if dialled is None:
            await self._reply(writer, 502, "upstream unreachable")
            return
        up_reader, up_writer = dialled
        self.stats["allowed_connect"] += 1
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    # ---- plain HTTP forwarding ---------------------------------------------------------------------
    async def _forward(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, method: bytes,
                       target: bytes, header_lines: list[bytes]) -> None:
        rest = target[len(b"http://"):]
        authority, _, path = rest.partition(b"/")
        split = self._split(authority, 80)
        vetted = await self._vet(*split) if split else None
        if split is None:
            self.stats["denied_malformed"] += 1
        if vetted is None or split is None:
            await self._reply(writer, 403, "egress denied")
            return
        kept = [line for line in header_lines if line.split(b":", 1)[0].strip().lower() not in HOP_BY_HOP]
        head = method + b" /" + path + b" HTTP/1.1\r\n" + b"\r\n".join(kept) + b"\r\nConnection: close\r\n\r\n"
        dialled = await self._dial(vetted, split[1])
        if dialled is None:
            await self._reply(writer, 502, "upstream unreachable")
            return
        up_reader, up_writer = dialled
        self.stats["allowed_http"] += 1
        up_writer.write(head)
        await up_writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    # ---- plumbing -----------------------------------------------------------------------------------------
    async def _pipe(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter,
                    u_reader: asyncio.StreamReader, u_writer: asyncio.StreamWriter) -> None:
        budget = [self.config.max_bytes_per_connection]

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    chunk = await asyncio.wait_for(src.read(65536), self.config.idle_timeout_s)
                    if not chunk:
                        break
                    budget[0] -= len(chunk)
                    if budget[0] < 0:
                        self.stats["byte_cap"] += 1
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (TimeoutError, ConnectionError, OSError):
                pass
            finally:
                try:
                    dst.close()
                except OSError:
                    pass

        await asyncio.gather(pump(c_reader, u_writer), pump(u_reader, c_writer))

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, text: str) -> None:
        body = text.encode()
        writer.write(f"HTTP/1.1 {status} {text}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
                     "Content-Type: text/plain\r\n\r\n".encode() + body)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
