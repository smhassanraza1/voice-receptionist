"""`python -m receptionist.billing_agent`: serve the billing agent with uvicorn."""

from __future__ import annotations

import logging

import uvicorn

from receptionist.billing_agent.config import BillingAgentSettings
from receptionist.billing_agent.server import build_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = BillingAgentSettings()
    logging.getLogger(__name__).info(
        "Agent card: %s.well-known/agent-card.json", settings.public_url
    )
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
