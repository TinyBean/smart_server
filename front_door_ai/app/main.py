import argparse
import asyncio
import logging
from pathlib import Path

import uvicorn

from .runtime import Runtime
from .web import create_apps


async def serve(args):
    runtime = Runtime(Path(args.data_dir), dev=args.dev)
    media, admin = create_apps(runtime)
    host = "127.0.0.1" if args.dev else "0.0.0.0"
    servers = [uvicorn.Server(uvicorn.Config(app, host=host, port=port, proxy_headers=False,
                                           access_log=False, log_level="warning"))
               for app, port in ((media, args.media_port), (admin, args.admin_port))]
    await runtime.start()
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), 10)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--dev", action="store_true", help="Bind loopback; bypass HA Ingress source check locally")
    parser.add_argument("--media-port", type=int, default=8080)
    parser.add_argument("--admin-port", type=int, default=8099)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
