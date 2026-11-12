"""Small helpers around LiveKit's provider-agnostic `llm.LLM` interface."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from livekit.agents import llm
from livekit.agents.metrics import ModelUsageCollector

# reasoning models reject any temperature other than the default
_FIXED_TEMPERATURE = re.compile(r"^(gpt-5|o\d)")


def _sampling(model: llm.LLM, temperature: float) -> dict[str, Any]:
    name = model.model.rsplit("/", 1)[-1]
    return {} if _FIXED_TEMPERATURE.match(name) else {"temperature": temperature}


async def complete(model: llm.LLM, *, system: str, user: str, temperature: float = 0.7) -> str:
    chat_ctx = llm.ChatContext()
    chat_ctx.add_message(role="system", content=system)
    chat_ctx.add_message(role="user", content=user)
    stream = model.chat(chat_ctx=chat_ctx, extra_kwargs=_sampling(model, temperature))
    parts: list[str] = []
    async with stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                parts.append(chunk.delta.content)
    return "".join(parts).strip()


async def call_structured(
    model: llm.LLM, *, system: str, user: str, tool: llm.FunctionTool
) -> dict[str, Any]:
    """Force a single tool call and return its parsed arguments (portable structured output)."""
    chat_ctx = llm.ChatContext()
    chat_ctx.add_message(role="system", content=system)
    chat_ctx.add_message(role="user", content=user)
    stream = model.chat(chat_ctx=chat_ctx, tools=[tool], tool_choice="required")
    arguments: str | None = None
    async with stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.tool_calls:
                arguments = chunk.delta.tool_calls[0].arguments
    if not arguments:
        raise RuntimeError("model returned no structured output")
    return json.loads(arguments)


@contextmanager
def metered(*models: llm.LLM | None) -> Iterator[ModelUsageCollector]:
    """Collect the token usage of every request these models make inside the block.

    The listeners come off again on exit, so a model shared across scenarios (the judge) is
    counted once per scenario.
    """
    collector = ModelUsageCollector()
    attached = list({id(m): m for m in models if m is not None}.values())
    for m in attached:
        m.on("metrics_collected", collector.collect)
    try:
        yield collector
    finally:
        for m in attached:
            m.off("metrics_collected", collector.collect)
