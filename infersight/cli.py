"""InferSight CLI.

    infersight run --upstream http://localhost:8000 --port 8020
    infersight discover
    infersight version
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from infersight import __version__
from infersight.config import InferSightConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="infersight", description="LLM inference observability sidecar")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Start the sidecar proxy")
    run.add_argument("--config", help="Path to JSON config file")
    run.add_argument("--upstream", dest="upstream_url", help="vLLM server URL (default http://localhost:8000)")
    run.add_argument("--host", dest="listen_host", help="Listen host (default 0.0.0.0)")
    run.add_argument("--port", dest="listen_port", type=int, help="Listen port (default 8020)")
    run.add_argument("--engine", help="Engine label: vllm | tgi | sglang (default vllm)")
    run.add_argument("--hosted-api-key", dest="hosted_api_key", help="Enable hosted tier shipping with this API key")
    run.add_argument("--hosted-url", dest="hosted_url", help="Hosted ingest URL override")

    disc = sub.add_parser("discover", help="Auto-discover running vLLM instances")
    disc.add_argument("--config", help="Path to JSON config file")
    disc.add_argument("--hosts", help="Comma-separated hosts to probe")
    disc.add_argument("--ports", help="Comma-separated ports to probe")

    sub.add_parser("version", help="Print version")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.command == "version":
        print(f"infersight {__version__}")
        return 0

    if args.command == "discover":
        overrides = {}
        if args.hosts:
            overrides["discovery_hosts"] = [h.strip() for h in args.hosts.split(",")]
        if args.ports:
            overrides["discovery_ports"] = [int(p) for p in args.ports.split(",")]
        config = InferSightConfig.load(config_file=args.config, overrides=overrides)

        from infersight.discovery import discover

        instances = asyncio.run(discover(config))
        if not instances:
            print("No inference servers found.", file=sys.stderr)
            return 1
        print(json.dumps([vars(i) for i in instances], indent=2))
        return 0

    if args.command == "run":
        overrides: dict = {
            k: getattr(args, k)
            for k in ("upstream_url", "listen_host", "listen_port", "engine")
            if getattr(args, k, None) is not None
        }
        if args.hosted_api_key:
            hosted = {"enabled": True, "api_key": args.hosted_api_key}
            if args.hosted_url:
                hosted["ingest_url"] = args.hosted_url
            overrides["hosted"] = hosted
        config = InferSightConfig.load(config_file=args.config, overrides=overrides)

        import uvicorn

        from infersight.proxy import create_app

        # ASCII-only banners: Windows consoles often use cp1252 and crash on
        # Unicode arrows (e.g. U+2192), which would prevent the proxy from starting.
        print(f"InferSight {__version__}")
        print(f"  proxying   http://{config.listen_host}:{config.listen_port}  ->  {config.upstream_url}")
        print(f"  metrics    http://{config.listen_host}:{config.listen_port}{config.metrics_path}")
        if config.hosted.enabled:
            print(f"  hosted     shipping to {config.hosted.ingest_url}")
        uvicorn.run(create_app(config), host=config.listen_host, port=config.listen_port, log_level="warning")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
