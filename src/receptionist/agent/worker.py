"""LiveKit worker: one `AgentSession` per call, from the browser (WebRTC) or a phone (SIP)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import NOT_GIVEN, AgentServer, AgentSession, JobContext, room_io

from receptionist.billing_agent.client import BillingAgentClient

from .agents import ReceptionistAgent
from .events import CallRecord, EventPublisher
from .handoff import HumanHandoff
from .limits import USER_AWAY_SECONDS, CallLimits
from .multilingual import LanguageFollower
from .pipelines import build_components
from .settings import CallConfig, get_settings
from .state import CallState
from .telemetry import setup_tracing
from .tools import build_mcp_toolset

load_dotenv()
logger = logging.getLogger("receptionist.agent")

settings = get_settings()
tracing = setup_tracing()  # module scope: shared by jobs running in thread mode
server = AgentServer()


@server.rtc_session(agent_name=settings.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    config = CallConfig.from_metadata(ctx.job.metadata, settings)
    ctx.log_context_fields = {"room": ctx.room.name, "mode": config.mode}

    state = CallState(
        room_name=ctx.room.name,
        language=config.language if config.language != "auto" else "en",
    )
    components = build_components(config, settings)
    record = CallRecord(mode=config.mode, models=components.labels)
    publisher = EventPublisher(ctx.room, record)
    handoff = HumanHandoff(ctx, settings, publisher)
    billing = BillingAgentClient(settings.billing_agent_url, settings.billing_agent_token)

    session: AgentSession[CallState] = AgentSession(
        **components.session_kwargs(),
        userdata=state,
        max_tool_steps=4,
        aec_warmup_duration=2.0,
        user_away_timeout=USER_AWAY_SECONDS,
    )
    publisher.attach(session)
    handoff.watch(session)
    CallLimits(ctx, session, max_call_seconds=settings.max_call_seconds).attach()
    LanguageFollower(
        session, switch_tts=config.mode == "pipeline" and config.language == "auto"
    ).attach()
    if tracing is not None:
        tracing.watch(ctx, session, realtime=components.stt is None)

    async def on_shutdown() -> None:
        if state.billing_context_id:
            await billing.close_context(state.billing_context_id)
        await billing.aclose()
        if log_dir := os.getenv("RECEPTIONIST_CALL_LOG_DIR"):
            path = Path(log_dir) / f"{ctx.room.name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record.to_dict(), indent=2))

    ctx.add_shutdown_callback(on_shutdown)

    agent = ReceptionistAgent(
        clinic_tools=build_mcp_toolset(settings, state),
        billing=billing,
        handoff=handoff,
        language=config.language,
    )
    await session.start(
        agent=agent,
        room=ctx.room,
        record=tracing.record_options(ctx) if tracing is not None else NOT_GIVEN,
        room_options=room_io.RoomOptions(
            # keep the room alive after the agent leaves so a human operator can take over
            delete_room_on_close=False,
        ),
    )
    publisher.session_info(config.language)
