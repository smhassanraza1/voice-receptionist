"""A scripted LiveKit LLM for offline tests: a function decides each reply from the context."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from livekit.agents.llm import (
    LLM,
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    CompletionUsage,
    FunctionToolCall,
    LLMStream,
    Tool,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions
from livekit.agents.utils import shortuuid


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


Script = Callable[[ChatContext, list[Tool]], Reply]


class ScriptedLLM(LLM):
    """`usage` (prompt, completion) tokens are reported on every reply, as a real API would."""

    def __init__(
        self, script: Script, *, model: str = "scripted", usage: tuple[int, int] | None = None
    ) -> None:
        super().__init__()
        self._script = script
        self._model = model
        self.usage = usage
        self.calls: list[ChatContext] = []
        self.extra_kwargs: list[Any] = []

    @property
    def model(self) -> str:
        return self._model

    def chat(
        self,
        *,
        chat_ctx: ChatContext,
        tools: list[Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls: Any = NOT_GIVEN,
        tool_choice: Any = NOT_GIVEN,
        extra_kwargs: Any = NOT_GIVEN,
    ) -> LLMStream:
        self.calls.append(chat_ctx.copy())
        self.extra_kwargs.append(extra_kwargs)
        return _ScriptedStream(
            self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options
        )


class _ScriptedStream(LLMStream):
    async def _run(self) -> None:
        reply = self._llm._script(self.chat_ctx, self._tools)  # type: ignore[attr-defined]
        if reply.text:
            self._event_ch.send_nowait(
                ChatChunk(id=shortuuid(), delta=ChoiceDelta(role="assistant", content=reply.text))
            )
        if reply.tool_calls:
            calls = [
                FunctionToolCall(name=name, arguments=json.dumps(args), call_id=shortuuid("call_"))
                for name, args in reply.tool_calls
            ]
            self._event_ch.send_nowait(
                ChatChunk(id=shortuuid(), delta=ChoiceDelta(role="assistant", tool_calls=calls))
            )
        if self._llm.usage:  # type: ignore[attr-defined]
            prompt, completion = self._llm.usage  # type: ignore[attr-defined]
            self._event_ch.send_nowait(
                ChatChunk(
                    id=shortuuid(),
                    usage=CompletionUsage(
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        total_tokens=prompt + completion,
                    ),
                )
            )


def last_item(chat_ctx: ChatContext) -> Any:
    return chat_ctx.items[-1]


def last_user_text(chat_ctx: ChatContext) -> str:
    for item in reversed(chat_ctx.items):
        if item.type == "message" and item.role == "user":
            return item.text_content or ""
    return ""
