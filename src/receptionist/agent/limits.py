"""Ends calls that run too long or that the caller has walked away from.

Every call has a hard cap (`RECEPTIONIST_MAX_CALL_SECONDS`), after which the agent says goodbye
and hangs up, so a forgotten tab or an abused public demo can't keep the models running. When
the caller has been silent for `USER_AWAY_SECONDS`, the agent asks whether they are still there
and hangs up if the silence lasts as long again. A caller waiting for a staff member is never
hung up on for being quiet.
"""

from __future__ import annotations

import asyncio
import logging

from livekit.agents import AgentSession, JobContext
from livekit.agents.voice.events import CloseEvent, UserStateChangedEvent

from .state import CallState

logger = logging.getLogger("receptionist.agent")

USER_AWAY_SECONDS = 20.0

_TIME_UP = (
    "Tell the caller, in one short sentence, that this call has reached its time limit, thank "
    "them for calling Lakeside Family Clinic, and say goodbye."
)
_CHECK_IN = "The caller has gone quiet. Ask, in one short sentence, whether they are still there."
_GONE = (
    "The caller hasn't answered. Say, in one short sentence, that you'll end the call now and "
    "they're welcome to call back anytime, then say goodbye."
)


class CallLimits:
    def __init__(
        self,
        job_ctx: JobContext | None,
        session: AgentSession[CallState],
        *,
        max_call_seconds: float,
        user_away_seconds: float = USER_AWAY_SECONDS,
    ) -> None:
        self._job_ctx = job_ctx
        self._session = session
        self._max_call_seconds = max_call_seconds
        self._user_away_seconds = user_away_seconds
        self._time_up: asyncio.TimerHandle | None = None
        self._away: asyncio.TimerHandle | None = None
        self._hang_up_task: asyncio.Task[None] | None = None
        self._closed = False

    def attach(self) -> None:
        self._session.on("user_state_changed", self._on_user_state)
        self._session.on("close", self._on_close)
        if self._max_call_seconds > 0:
            self._time_up = asyncio.get_running_loop().call_later(
                self._max_call_seconds, self._hang_up, _TIME_UP, "max call duration reached"
            )

    def _on_user_state(self, ev: UserStateChangedEvent) -> None:
        if ev.old_state == "away" and self._away is not None:
            self._away.cancel()
            self._away = None
        if ev.new_state != "away" or self._closed or self._hang_up_task is not None:
            return
        if self._session.userdata.escalated:
            return
        self._session.generate_reply(instructions=_CHECK_IN, tool_choice="none")
        self._away = asyncio.get_running_loop().call_later(
            self._user_away_seconds, self._hang_up, _GONE, "caller away"
        )

    def _on_close(self, ev: CloseEvent) -> None:
        self._closed = True
        for timer in (self._time_up, self._away):
            if timer is not None:
                timer.cancel()

    def _hang_up(self, instructions: str, reason: str) -> None:
        if self._closed or self._hang_up_task is not None:
            return
        self._hang_up_task = asyncio.create_task(self._say_goodbye(instructions, reason))

    async def _say_goodbye(self, instructions: str, reason: str) -> None:
        logger.info("ending the call", extra={"reason": reason})
        try:
            await self._session.generate_reply(
                instructions=instructions, tool_choice="none", allow_interruptions=False
            )
            # closing the session first publishes the call's usage while the room is connected
            await self._session.aclose()
        except Exception:
            logger.exception("could not say goodbye before ending the call")
        finally:
            if self._job_ctx is not None:
                job_ctx = self._job_ctx

                async def delete_room() -> None:
                    await job_ctx.delete_room()

                job_ctx.add_shutdown_callback(delete_room)
                job_ctx.shutdown(reason=reason)
