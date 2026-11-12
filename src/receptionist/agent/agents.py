"""The receptionist and the billing liaison.

Billing questions are answered by a separate billing agent reached over A2A. The liaison is a
LiveKit agent handoff: it keeps the caller's voice session, forwards each billing question to
the remote agent (reusing one A2A context per call), and hands back to reception when done.
"""

from __future__ import annotations

import logging

from livekit.agents import Agent, RunContext, function_tool, llm
from livekit.agents.beta import EndCallTool

from receptionist.billing_agent.client import BillingAgentClient

from .handoff import HumanHandoff
from .prompts import billing_liaison_instructions, receptionist_instructions
from .state import CallState

logger = logging.getLogger("receptionist.agent")

STAY_ON_THE_LINE = (
    "Don't end the call: a staff member is on the way. Tell the caller they'll join shortly and "
    "stay with them until then."
)


class _EndCall(EndCallTool):
    """`end_call`, except while the caller waits for a staff member: hanging up then would
    delete the room the operator is about to join."""

    async def _end_call(self, ctx: RunContext[CallState]) -> str | None:
        if ctx.userdata.escalated:
            return STAY_ON_THE_LINE
        return await super()._end_call(ctx)


class _CallAgent(Agent):
    def __init__(
        self,
        *,
        instructions: str,
        handoff: HumanHandoff,
        tools: list[llm.Tool | llm.Toolset],
        chat_ctx: llm.ChatContext | None = None,
    ) -> None:
        super().__init__(
            instructions=instructions,
            tools=[*tools, _EndCall(delete_room=True, ignore_on_enter=True)],
            chat_ctx=chat_ctx,
        )
        self._handoff = handoff

    @function_tool
    async def transfer_to_human(
        self, context: RunContext[CallState], reason: str, summary: str
    ) -> str | None:
        """Transfer the caller to a human staff member. Call only after the caller agreed.

        Args:
            reason: Short reason, e.g. "caller asked for a person" or "billing dispute".
            summary: One or two sentences for the staff member: who is calling and what they need.
        """
        return await self._handoff.to_human(self, context, reason=reason, summary=summary)


class ReceptionistAgent(_CallAgent):
    def __init__(
        self,
        *,
        clinic_tools: llm.Toolset,
        billing: BillingAgentClient,
        handoff: HumanHandoff,
        language: str,
        chat_ctx: llm.ChatContext | None = None,
        greet: bool = True,
    ) -> None:
        super().__init__(
            instructions=receptionist_instructions(language),
            handoff=handoff,
            tools=[clinic_tools],
            chat_ctx=chat_ctx,
        )
        self._clinic_tools = clinic_tools
        self._billing = billing
        self._language = language
        self._greet = greet

    async def on_enter(self) -> None:
        if not self._greet:
            self.session.generate_reply(
                instructions="Ask if there is anything else you can help with."
            )
            return
        self.session.generate_reply(
            instructions=(
                "Greet the caller warmly in one sentence: thank them for calling Lakeside "
                "Family Clinic, say you are Maya, the virtual receptionist, and ask how you "
                "can help."
            ),
            # A greeting never needs a lookup; small realtime models otherwise open the
            # call by searching the knowledge base and reading out the clinic's details.
            tool_choice="none",
        )

    @function_tool
    async def transfer_to_billing(self, context: RunContext[CallState]) -> tuple[Agent, str]:
        """Hand the caller to the billing desk for balances, invoices, insurance claims or
        payment plans. Verify identity with lookup_appointments first for account questions."""
        state = context.userdata
        liaison = BillingLiaisonAgent(
            reception=self,
            billing=self._billing,
            handoff=self._handoff,
            patient_name=state.verified_patient_name,
            language=state.language,
            chat_ctx=self.chat_ctx,
        )
        return liaison, "Connecting the caller to billing."


class BillingLiaisonAgent(_CallAgent):
    def __init__(
        self,
        *,
        reception: ReceptionistAgent,
        billing: BillingAgentClient,
        handoff: HumanHandoff,
        patient_name: str | None,
        language: str,
        chat_ctx: llm.ChatContext,
    ) -> None:
        super().__init__(
            instructions=billing_liaison_instructions(patient_name, language),
            handoff=handoff,
            tools=[],
            chat_ctx=chat_ctx,
        )
        self._reception = reception
        self._billing = billing

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="Tell the caller you can help with billing, then answer their billing "
            "question by calling ask_billing_department."
        )

    @function_tool
    async def ask_billing_department(self, context: RunContext[CallState], question: str) -> str:
        """Ask the billing department a question on the caller's behalf.

        Args:
            question: The caller's billing question, in their own words.
        """
        state = context.userdata
        reply = await self._billing.ask(
            question,
            patient_id=state.verified_patient_id,
            verified=state.verified_patient_id is not None,
            language=state.language,
            context_id=state.billing_context_id,
        )
        state.billing_context_id = reply.context_id or state.billing_context_id
        return reply.text

    @function_tool
    async def return_to_reception(self, context: RunContext[CallState]) -> tuple[Agent, str]:
        """Go back to the receptionist when billing is done or the caller asks about
        appointments or general clinic questions."""
        agent = ReceptionistAgent(
            clinic_tools=self._reception._clinic_tools,
            billing=self._billing,
            handoff=self._handoff,
            language=context.userdata.language,
            chat_ctx=self.chat_ctx,
            greet=False,
        )
        return agent, "Back at reception."
