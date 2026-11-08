"""Entry point: `egressproxy --allow api.anthropic.com:443 --allow-private ingest.internal:8000`."""

import argparse
import asyncio
import logging

from .proxy import Allow, EgressProxy, ProxyConfig


def parse_allow(text: str, *, private: bool) -> Allow:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"expected host:port, got {text!r}")
    return Allow(host.lower(), int(port), private)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="egressproxy")
    p.add_argument("--listen", default="0.0.0.0:3128")
    p.add_argument("--allow", action="append", default=[], help="public host:port (private addresses refused)")
    p.add_argument("--allow-private", action="append", default=[], help="host:port that may resolve to private addresses")
    p.add_argument("--max-connections", type=int, default=64)
    args = p.parse_args(argv)
    allow = tuple(parse_allow(a, private=False) for a in args.allow) + tuple(
        parse_allow(a, private=True) for a in args.allow_private)
    if not allow:
        p.error("at least one --allow is required (default deny)")
    host, _, port = args.listen.rpartition(":")
    logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
    proxy = EgressProxy(ProxyConfig(allow, max_connections=args.max_connections))

    async def run() -> None:
        server = await proxy.start(host, int(port))
        async with server:
            await server.serve_forever()

    asyncio.run(run())
