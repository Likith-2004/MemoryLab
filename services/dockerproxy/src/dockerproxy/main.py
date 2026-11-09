"""Entry point: `dockerproxy --listen 0.0.0.0:2375 --socket /var/run/docker.sock --image memorylab-worker:dev --network memorylab-sandbox`."""

import argparse
import asyncio
import logging

from .policy import Policy
from .server import Proxy


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="dockerproxy")
    p.add_argument("--listen", default="0.0.0.0:2375")
    p.add_argument("--socket", default="/var/run/docker.sock")
    p.add_argument("--image", action="append", required=True, help="an image reference a sandbox may be started from (repeatable)")
    p.add_argument("--network", required=True, help="the one network a sandbox may join")
    p.add_argument("--runtime", action="append", default=[], help="an OCI runtime besides the default (e.g. runsc)")
    p.add_argument("--allow-env", action="append", default=[], help="an environment variable name a sandbox may be given (never a secret)")
    p.add_argument("--max-connections", type=int, default=32)
    args = p.parse_args(argv)
    host, _, port = args.listen.rpartition(":")
    logging.basicConfig(level=logging.INFO, format='{"t":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')
    policy = Policy(images=frozenset(args.image), network=args.network, runtimes=frozenset({"", *args.runtime}),
                    env_names=frozenset(args.allow_env))
    proxy = Proxy(policy, args.socket, max_connections=args.max_connections)

    async def run() -> None:
        server = await proxy.start(host, int(port))
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
