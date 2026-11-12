"""Connects the agent to the clinic MCP server."""

from __future__ import annotations

import json
import logging
from typing import Any

from livekit.agents import mcp

from .settings import AgentSettings
from .state import CallState

logger = logging.getLogger("receptionist.agent")

MCP_TOOLS = (
    "search_knowledge_base",
    "list_providers",
    "find_available_slots",
    "book_appointment",
    "lookup_appointments",
    "reschedule_appointment",
    "cancel_appointment",
)

# Tools whose result can prove who the caller is; the server says so with identity_verified.
_IDENTITY_TOOLS = {"lookup_appointments", "book_appointment"}


def _payload(result: Any) -> Any:
    if result.structured_content is not None:
        return result.structured_content
    texts = [part.text for part in result.content if hasattr(part, "text")]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        return joined


def make_result_resolver(state: CallState) -> mcp.MCPToolResultResolver:
    def resolve(ctx: mcp.MCPToolResultContext) -> str:
        payload = _payload(ctx.result)
        if ctx.tool_name in _IDENTITY_TOOLS and isinstance(payload, dict):
            patient_id = payload.get("patient_id")
            if patient_id and payload.get("identity_verified") is True:
                state.mark_verified(patient_id, payload.get("patient_name"))
        # compact JSON keeps per-turn tokens down (realtime models re-bill context each turn)
        return payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))

    return resolve


def build_mcp_toolset(settings: AgentSettings, state: CallState) -> mcp.MCPToolset:
    headers = (
        {"Authorization": f"Bearer {settings.mcp_auth_token}"} if settings.mcp_auth_token else {}
    )
    server = mcp.MCPServerHTTP(
        url=settings.mcp_server_url,
        transport_type="streamable_http",
        allowed_tools=list(MCP_TOOLS),
        headers=headers,
        timeout=5,
        client_session_timeout_seconds=10,
        tool_result_resolver=make_result_resolver(state),
    )
    return mcp.MCPToolset(
        id="clinic",
        mcp_server=server,
        tool_options={
            # writes must not be fired twice when the model retries mid-turn
            "book_appointment": {"on_duplicate": "reject", "duplicate_scope": "name_and_args"},
            "reschedule_appointment": {
                "on_duplicate": "reject",
                "duplicate_scope": "name_and_args",
            },
            "cancel_appointment": {"on_duplicate": "reject", "duplicate_scope": "name_and_args"},
        },
    )
