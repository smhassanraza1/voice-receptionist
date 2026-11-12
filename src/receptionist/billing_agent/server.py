"""Agent card and ASGI app factory for the billing agent.

`build_app(settings)` returns a Starlette app serving:
  * GET  /.well-known/agent-card.json  - public agent card
  * POST /                             - A2A JSON-RPC 1.0 (bearer token required)

The token is shared by one trusted client, the voice receptionist. Tasks are kept only as long
as conversations (`session_ttl_s`) and cannot be listed; see `task_store`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from a2a.utils import AGENT_CARD_WELL_KNOWN_PATH, TransportProtocol
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware

from receptionist.billing_agent.config import BillingAgentSettings
from receptionist.billing_agent.engine import ReasoningEngine
from receptionist.billing_agent.executor import BillingAgentExecutor
from receptionist.billing_agent.ledger import BillingLedger
from receptionist.billing_agent.rules import RuleBasedEngine
from receptionist.billing_agent.security import BearerTokenBackend, unauthorized
from receptionist.billing_agent.sessions import SessionStore
from receptionist.billing_agent.task_store import ExpiringTaskStore
from receptionist.common.fixtures import CLINIC

logger = logging.getLogger(__name__)

SKILL_ID = "billing_inquiry"
SECURITY_SCHEME = "bearer"
_MEDIA_TYPES = ["text/plain", "application/json"]


def _package_version() -> str:
    try:
        return version("voice-receptionist")
    except PackageNotFoundError:  # running from a source tree without an install
        return "0.0.0"


def build_agent_card(settings: BillingAgentSettings) -> AgentCard:
    return AgentCard(
        name=f"{CLINIC['name']} Billing Agent",
        description=(
            "Answers billing questions relayed by the clinic's voice receptionist: balances "
            "and invoices, insurance claim status, payment plans, accepted insurance, and "
            "secure payment links. Replies are short and written to be spoken aloud. "
            "Account details are shared only for callers the receptionist has verified."
        ),
        version=_package_version(),
        provider=AgentProvider(organization=CLINIC["name"], url="https://lakeside-clinic.example"),
        supported_interfaces=[
            AgentInterface(
                url=settings.public_url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=_MEDIA_TYPES,
        default_output_modes=_MEDIA_TYPES,
        skills=[
            AgentSkill(
                id=SKILL_ID,
                name="Billing inquiry",
                description=(
                    "Balances, invoice breakdowns, insurance claim status, payment plan "
                    "eligibility, accepted insurance, and texting a secure payment link. Send "
                    "the caller's question as a text part plus a data part "
                    '{"patient_id": "P-1002", "verified": true, "language": "en"}.'
                ),
                tags=["billing", "insurance", "payments", "healthcare"],
                examples=[
                    "What is my current balance?",
                    "Can you break down my last bill?",
                    "What's the status of my insurance claim?",
                    "Am I eligible for a payment plan?",
                    "Which insurance plans do you accept?",
                    "Can you text me a link to pay?",
                    "¿Cuánto debo?",
                ],
                input_modes=_MEDIA_TYPES,
                output_modes=_MEDIA_TYPES,
            )
        ],
        security_schemes={
            SECURITY_SCHEME: SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="Bearer",
                    description="Shared secret from BILLING_AGENT_TOKEN.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={SECURITY_SCHEME: StringList()})],
    )


def build_engine(settings: BillingAgentSettings, ledger: BillingLedger) -> ReasoningEngine:
    """The OpenAI engine when a key is configured (with rules as its fallback), else rules."""
    rules = RuleBasedEngine(ledger)
    api_key = settings.openai_api_key
    if settings.llm == "off" or api_key is None:
        logger.info("Billing agent reasoning: rule-based")
        return rules

    # Imported lazily: the openai package is only needed in LLM mode.
    from openai import AsyncOpenAI

    from receptionist.billing_agent.llm import OpenAIEngine

    logger.info("Billing agent reasoning: OpenAI %s", settings.model)
    return OpenAIEngine(
        ledger,
        client=AsyncOpenAI(
            api_key=api_key.get_secret_value(), timeout=settings.llm_timeout_s, max_retries=1
        ),
        model=settings.model,
        fallback=rules,
        timeout_s=settings.llm_timeout_s,
        reasoning_effort=settings.reasoning_effort,
    )


def build_app(
    settings: BillingAgentSettings,
    *,
    ledger: BillingLedger | None = None,
    engine: ReasoningEngine | None = None,
) -> Starlette:
    """The billing agent as an ASGI app. `ledger` and `engine` are injectable for tests."""
    ledger = ledger or BillingLedger()
    card = build_agent_card(settings)
    executor = BillingAgentExecutor(
        engine or build_engine(settings, ledger),
        SessionStore(
            ttl_s=settings.session_ttl_s,
            max_sessions=settings.max_sessions,
            max_turns=settings.max_history_turns,
        ),
    )
    task_store = ExpiringTaskStore(
        ttl_s=settings.session_ttl_s,
        # About one task per turn, so this covers the history of every live session.
        max_tasks=settings.max_sessions * settings.max_history_turns,
    )
    handler = DefaultRequestHandler(agent_executor=executor, task_store=task_store, agent_card=card)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        await handler.aclose()  # drain in-flight tasks on shutdown

    app = Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/"),
        ],
        middleware=[
            Middleware(
                AuthenticationMiddleware,
                backend=BearerTokenBackend(
                    settings.token.get_secret_value(),
                    public_paths={AGENT_CARD_WELL_KNOWN_PATH},
                    principal="voice-receptionist",
                ),
                on_error=unauthorized,
            )
        ],
        lifespan=lifespan,
    )
    app.state.request_handler = handler
    app.state.ledger = ledger
    return app
