"""Serve the clinic MCP server over streamable HTTP: `python -m receptionist.mcp_server`."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

import uvicorn

from .db import reset_database
from .security import audit_logger
from .server import build_app
from .settings import Settings


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Audit events are already JSON, so they go out as bare JSON lines, whatever the log level.
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset-db", action="store_true", help="re-seed the database from the fixtures first"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    configure_logging(settings.log_level)
    if settings.jwt_secret is None:
        sys.exit(
            "MCP_JWT_SECRET is not set. Generate one with:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    if args.reset_db:
        reset_database(settings.db_path)
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        server_header=False,
    )


if __name__ == "__main__":
    main()
