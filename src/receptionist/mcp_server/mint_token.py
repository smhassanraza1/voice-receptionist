"""Print a bearer token the MCP server accepts.

    python -m receptionist.mcp_server.mint_token --sub voice-agent [--scopes kb:read ...]
        [--ttl-hours 12]

Signs with MCP_JWT_SECRET for the audience MCP_SERVER_URL (both read from the environment).
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from datetime import timedelta

from .auth import ALL_SCOPES, mint_token
from .settings import Settings

MAX_TTL_HOURS = 24 * 30


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Mint a bearer token for the clinic MCP server.")
    parser.add_argument("--sub", required=True, help="client id, e.g. voice-agent")
    parser.add_argument(
        "--scopes",
        nargs="+",
        default=list(ALL_SCOPES),
        help=f"space or comma separated (default: all of {' '.join(ALL_SCOPES)})",
    )
    parser.add_argument("--ttl-hours", type=float, default=12.0)
    parser.add_argument("--audience", help="defaults to MCP_SERVER_URL")
    args = parser.parse_args(argv)

    scopes = [s for raw in args.scopes for s in re.split(r"[\s,]+", raw) if s]
    if unknown := sorted(set(scopes) - set(ALL_SCOPES)):
        parser.error(f"unknown scopes: {', '.join(unknown)}")
    if not 0 < args.ttl_hours <= MAX_TTL_HOURS:
        parser.error(f"--ttl-hours must be between 0 and {MAX_TTL_HOURS}")
    settings = Settings()
    if settings.jwt_secret is None:
        parser.error("MCP_JWT_SECRET is not set")

    print(
        mint_token(
            settings.jwt_secret.get_secret_value(),
            subject=args.sub,
            audience=args.audience or settings.server_url,
            scopes=scopes,
            ttl=timedelta(hours=args.ttl_hours),
        )
    )


if __name__ == "__main__":
    main()
