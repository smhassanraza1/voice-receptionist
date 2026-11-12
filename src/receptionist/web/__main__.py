"""`python -m receptionist.web` — run the web/token server with uvicorn."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .settings import WebSettings


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m receptionist.web",
        description="Token server, escalation API and frontend host for the voice receptionist.",
    )
    parser.add_argument("--host", help="bind address (default: $WEB_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default: $WEB_PORT or 8000)")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = WebSettings()
    uvicorn.run(
        create_app(settings),
        host=args.host or settings.web_host,
        port=args.port or settings.web_port,
        log_level=args.log_level,
        # Trust X-Forwarded-For only from a proxy on this machine (uvicorn's default allow-list),
        # so the per-IP rate limits see real client addresses behind a local reverse proxy.
        proxy_headers=True,
        # Operator consoles hold SSE streams open indefinitely; give them this long, then cancel.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
