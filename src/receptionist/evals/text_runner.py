"""Text channel: simulated caller <-> the real agent classes, tools and services, no audio.

This isolates the "brain" of each architecture (instruction following, tool use, policy) from
the audio path, and is cheap enough to run the full scenario suite on every change.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from livekit.agents import AgentSession, llm
from livekit.agents.testing import fake_job_context
from livekit.agents.voice.run_result import (
    AgentHandoffEvent,
    ChatMessageEvent,
    FunctionCallEvent,
    FunctionCallOutputEvent,
    RunEvent,
)

from receptionist.agent.agents import BillingLiaisonAgent, ReceptionistAgent
from receptionist.agent.events import CallRecord, EventPublisher
from receptionist.agent.handoff import HumanHandoff
from receptionist.agent.settings import AgentSettings
from receptionist.agent.state import CallState
from receptionist.agent.tools import build_mcp_toolset
from receptionist.billing_agent.client import BillingAgentClient

from .architectures import ArchName, call_config, model_label, text_model
from .cost import usage_record
from .grading import grade, snapshot_outcomes
from .llm_utils import metered
from .results import ScenarioResult
from .scenarios import Scenario
from .scoring import Event, format_conversation
from .services import EvalServices
from .simulator import CallerSimulator

logger = logging.getLogger("receptionist.evals")


def to_events(run_events: list[RunEvent]) -> list[Event]:
    events: list[Event] = []
    for ev in run_events:
        if isinstance(ev, ChatMessageEvent) and ev.item.role == "assistant":
            if text := ev.item.text_content:
                events.append(Event("agent", text, metrics=dict(ev.item.metrics)))
        elif isinstance(ev, FunctionCallEvent):
            try:
                args = json.loads(ev.item.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": ev.item.arguments}
            events.append(Event("tool_call", name=ev.item.name, args=args, call_id=ev.item.call_id))
        elif isinstance(ev, FunctionCallOutputEvent):
            events.append(
                Event(
                    "tool_result",
                    ev.item.output,
                    name=ev.item.name,
                    is_error=ev.item.is_error,
                    call_id=ev.item.call_id,
                )
            )
        elif isinstance(ev, AgentHandoffEvent):
            target = "billing" if isinstance(ev.new_agent, BillingLiaisonAgent) else "reception"
            events.append(Event("handoff", name=target))
    return events


def eval_agent_settings(base: AgentSettings, services: EvalServices, subject: str) -> AgentSettings:
    return base.model_copy(
        update={
            "mcp_server_url": services.mcp_url,
            "mcp_auth_token": services.mint_token(subject),
            "billing_agent_url": services.billing_url,
            "billing_agent_token": services.billing_token,
            "web_base_url": services.web_url,
            "internal_api_token": services.internal_token,
        }
    )


async def run_text_scenario(
    scenario: Scenario,
    arch: ArchName,
    services: EvalServices,
    *,
    base_settings: AgentSettings,
    simulator_llm: llm.LLM,
    judge_llm: llm.LLM | None,
    agent_llm: llm.LLM | llm.RealtimeModel | None = None,
) -> ScenarioResult:
    started = time.perf_counter()
    subject = f"eval-{arch}-{scenario.id}"
    settings = eval_agent_settings(base_settings, services, subject)
    language = scenario.persona.language if scenario.persona.language != "en" else "auto"
    config = call_config(arch, language)

    befores: list[list[dict[str, Any]]] = []
    events: list[Event] = []
    error: str | None = None

    state = CallState(room_name=f"eval-{scenario.id}", language=scenario.persona.language)
    publisher = EventPublisher(
        None, CallRecord(mode=config.mode, models={"llm": model_label(arch, settings)})
    )
    handoff = HumanHandoff(None, settings, publisher)
    billing = BillingAgentClient(settings.billing_agent_url, settings.billing_agent_token)
    agent = ReceptionistAgent(
        clinic_tools=build_mcp_toolset(settings, state),
        billing=billing,
        handoff=handoff,
        language=config.language,
    )
    simulator = CallerSimulator(simulator_llm, scenario)
    session: AgentSession[CallState] | None = None

    with metered(simulator_llm, judge_llm) as overhead:
        try:
            services.reset()
            befores = await snapshot_outcomes(services, scenario)
            with fake_job_context(job_id=f"eval-{scenario.id}"):
                async with AgentSession[CallState](
                    llm=agent_llm or text_model(arch, settings), userdata=state, max_tool_steps=4
                ) as session:
                    greeting = await session.start(agent, capture_run=True)
                    if greeting is not None:
                        await greeting
                        events.extend(to_events(greeting.events))

                    for _ in range(scenario.max_turns):
                        # the caller hears the conversation, not the agent's tool traffic
                        utterance = await simulator.next_turn(format_conversation(events))
                        if utterance is None:
                            break
                        events.append(Event("caller", utterance))
                        result = await session.run(user_input=utterance)
                        events.extend(to_events(result.events))
                        if any(e.role == "tool_call" and e.name == "end_call" for e in events):
                            break
        except Exception as e:  # a crashed run is a failed run, not a crashed suite
            logger.exception("scenario %s failed", scenario.id)
            error = f"{type(e).__name__}: {e}"
        finally:
            await billing.aclose()

        ttft = [
            round(e.metrics["llm_node_ttft"] * 1000)
            for e in events
            if e.role == "agent" and "llm_node_ttft" in e.metrics
        ]
        graded = await grade(
            scenario,
            events,
            services=services,
            befores=befores,
            judge_llm=judge_llm,
            arch=arch,
            channel="text",
            model=model_label(arch, settings),
            duration_s=time.perf_counter() - started,
            error=error,
            latency_ms={"llm_ttft": ttft} if ttft else {},
        )
    agent_usage = [u.model_dump() for u in session.usage.model_usage] if session else []
    graded.usage = usage_record(agent_usage, overhead=[u.model_dump() for u in overhead.flatten()])
    return graded
