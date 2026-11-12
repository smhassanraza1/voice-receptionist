"""Hands the call to a person.

Phone callers are transferred over SIP: a warm transfer (the agent dials a staff member,
briefs them, then bridges the caller) when an outbound trunk is configured, otherwise a cold
SIP REFER transfer. Browser callers can't be transferred, so the agent files an escalation
that the operator console picks up; when an `operator-*` participant joins the room the agent
says goodbye and leaves the caller and the operator talking.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from livekit import api, rtc
from livekit.agents import Agent, AgentSession, JobContext, RunContext
from livekit.agents.beta.workflows import WarmTransferTask
from livekit.agents.llm import ToolError
from livekit.agents.voice import SpeechHandle

from .events import EventPublisher
from .settings import AgentSettings
from .state import CallState

logger = logging.getLogger("receptionist.agent")

OPERATOR_PREFIX = "operator-"
_SIP_URI_SCHEMES = ("sip:", "sips:")


def transfer_targets(number: str) -> tuple[str, str]:
    """`HUMAN_TRANSFER_NUMBER` as (cold, warm) dial targets.

    The number can be given as `+15550100000`, `tel:+15550100000` or a SIP URI. A cold
    transfer (SIP REFER) takes a `tel:` or `sip:` URI; a warm transfer dials out with
    `CreateSIPParticipant`, which takes a bare number or a SIP URI.
    """
    number = number.strip()
    if number.lower().startswith(_SIP_URI_SCHEMES):
        return number, number
    bare = number[4:].strip() if number.lower().startswith("tel:") else number
    return f"tel:{bare}", bare


def announce(session: AgentSession, text: str) -> SpeechHandle:
    """Speak a fixed line in either architecture.

    `say()` needs a TTS model, which speech-to-speech sessions don't have, so a realtime
    model is asked to read the line out instead.
    """
    if session.tts is not None:
        return session.say(text, allow_interruptions=False)
    return session.generate_reply(
        instructions=f'Say exactly this, and nothing else: "{text}"', tool_choice="none"
    )


class HumanHandoff:
    def __init__(
        self,
        job_ctx: JobContext | None,
        settings: AgentSettings,
        publisher: EventPublisher,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._job_ctx = job_ctx
        self._settings = settings
        self._publisher = publisher
        self._http_transport = http_transport
        self._session: AgentSession | None = None
        self._hand_over_task: asyncio.Task[None] | None = None

    def watch(self, session: AgentSession) -> None:
        self._session = session
        if self._job_ctx is not None:
            self._job_ctx.room.on("participant_connected", self._on_participant_connected)

    def sip_caller(self) -> rtc.RemoteParticipant | None:
        if self._job_ctx is None:
            return None
        for participant in self._job_ctx.room.remote_participants.values():
            if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
                return participant
        return None

    async def to_human(
        self, agent: Agent, ctx: RunContext[CallState], *, reason: str, summary: str
    ) -> str | None:
        state = ctx.userdata
        if state.escalated:
            return "A staff member was already requested. Reassure the caller they are on the way."
        state.escalated = True
        self._publisher.handoff("human", reason)

        caller = self.sip_caller()
        number = self._settings.human_transfer_number.strip()
        if caller is not None and number and self._job_ctx is not None:
            ctx.disallow_interruptions()
            cold_target, warm_target = transfer_targets(number)
            if self._settings.human_transfer_mode == "warm" and self._settings.sip_outbound_trunk:
                try:
                    await announce(
                        ctx.session, "Please hold while I connect you with a member of our team."
                    )
                    await WarmTransferTask(
                        sip_call_to=warm_target,
                        sip_trunk_id=self._settings.sip_outbound_trunk,
                        chat_ctx=agent.chat_ctx,
                    )
                except Exception:
                    state.escalated = False
                    raise
                ctx.session.shutdown()
                return None

            try:
                await announce(
                    ctx.session, "I'm transferring you to our front desk now. Please hold."
                )
                result = await self._job_ctx.transfer_sip_participant(
                    caller, cold_target, play_dialtone=False
                )
            except Exception as e:
                logger.exception("SIP transfer failed")
                state.escalated = False
                raise ToolError("The transfer did not go through.") from e
            # console jobs return a bare SIPParticipantInfo, which has no status
            status = getattr(result, "status", api.SIPTransferStatus.STS_TRANSFER_SUCCESSFUL)
            if status == api.SIPTransferStatus.STS_TRANSFER_FAILED:
                logger.warning(
                    "SIP transfer failed",
                    extra={"reason": api.SIPTransferReason.Name(result.reason)},
                )
                state.escalated = False
                raise ToolError("The transfer did not go through.")
            ctx.session.shutdown()
            return None

        if await self._file_escalation(state.room_name, reason=reason, summary=summary):
            return (
                "A staff member has been notified and will join this call shortly. Tell the "
                "caller, and keep helping them with anything simple while they wait."
            )
        state.escalated = False
        return (
            "The staff paging system is unavailable. Apologize, give the front desk number "
            "five five five, zero one zero, zero zero zero zero, and offer to keep helping."
        )

    async def _file_escalation(self, room: str, *, reason: str, summary: str) -> bool:
        url = f"{self._settings.web_base_url.rstrip('/')}/api/escalations"
        try:
            async with httpx.AsyncClient(timeout=3.0, transport=self._http_transport) as client:
                resp = await client.post(
                    url,
                    json={"room": room, "reason": reason[:200], "summary": summary[:500]},
                    headers={"X-Internal-Token": self._settings.internal_api_token},
                )
                resp.raise_for_status()
            return True
        except httpx.HTTPError:
            logger.warning("could not file escalation", exc_info=True)
            return False

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        if not participant.identity.startswith(OPERATOR_PREFIX) or self._session is None:
            return
        if self._hand_over_task is not None:  # a second operator joined: one goodbye is enough
            return
        assert self._job_ctx is not None
        self._job_ctx.room.off("participant_connected", self._on_participant_connected)
        name = participant.name or "a member of our team"
        self._publisher.handoff("human", "operator joined")
        self._hand_over_task = asyncio.create_task(self._hand_over(name))

    async def _hand_over(self, operator_name: str) -> None:
        assert self._session is not None and self._job_ctx is not None
        try:
            await self._session.interrupt(force=True)
            await announce(
                self._session,
                f"You're now connected with {operator_name} from our front desk. Take care!",
            )
            # the room stays open (delete_room_on_close=False), so the agent leaving it keeps
            # the caller and the operator connected
            await self._session.aclose()
        except Exception:
            logger.exception("operator hand-over failed")
        finally:
            self._job_ctx.shutdown(reason="operator took over")
