"""Billing behaviour end to end: BillingAgentClient -> A2A JSON-RPC -> executor -> rule engine."""

from __future__ import annotations

import re

import httpx
import pytest
from starlette.applications import Starlette

from receptionist.billing_agent.client import BillingAgentClient
from receptionist.common.fixtures import ACCEPTED_INSURANCE, BILLING_ACCOUNTS

from ._support import TOKEN

pytestmark = pytest.mark.usefixtures("app")

# Account fixtures used below (see receptionist.common.fixtures).
WHITFIELD = "P-1002"  # $185.40 open on the echocardiogram, follow-up paid in full
RAHMAN = "P-1003"  # claim CLM-8801 in review
ONEILL = "P-1005"  # already on a payment plan, $59.00 left


def _balance(patient_id: str) -> float:
    account = next(a for a in BILLING_ACCOUNTS if a.patient_id == patient_id)
    return round(sum(invoice.balance for invoice in account.invoices), 2)


async def test_verified_balance(billing: BillingAgentClient) -> None:
    assert _balance(WHITFIELD) == 185.40  # guard against fixture drift

    reply = await billing.ask("How much do I owe?", patient_id=WHITFIELD, verified=True)

    assert reply.text == (
        "Your current balance is $185.40, for your echocardiogram on September 1. "
        "If you'd like, I can text you a secure payment link."
    )
    assert reply.data["state"] == "completed"
    assert reply.data["intent"] == "balance"
    assert reply.data["engine"] == "rules"  # no LLM in tests
    assert reply.data["facts"]["balance_due"] == 185.40
    assert reply.context_id


@pytest.mark.parametrize(
    ("patient_id", "verified"), [(WHITFIELD, False), (None, True), (None, False)]
)
async def test_unverified_callers_get_no_account_data(
    billing: BillingAgentClient, patient_id: str | None, verified: bool
) -> None:
    reply = await billing.ask(
        "What's my balance and what was it for?", patient_id=patient_id, verified=verified
    )

    assert "verify your identity" in reply.text
    assert not re.search(r"\d", reply.text)
    assert reply.data["needs_verification"] is True
    assert reply.data["verified"] is False
    assert reply.data["facts"] == {}


async def test_unverified_callers_still_get_general_information(
    billing: BillingAgentClient,
) -> None:
    plans = await billing.ask("Which insurance do you take?", patient_id=None, verified=False)
    policy = await billing.ask("Do you offer payment plans?", patient_id=None, verified=False)

    assert plans.text == f"We accept {', '.join(ACCEPTED_INSURANCE[:-1])}, and Summit Mutual EPO."
    assert plans.data["needs_verification"] is False
    assert policy.text.startswith("We offer payment plans for balances of $50.00 or more")
    assert "verify your identity" in policy.text


async def test_claim_status(billing: BillingAgentClient) -> None:
    reply = await billing.ask(
        "Has my insurance claim gone through?", patient_id=RAHMAN, verified=True
    )

    assert reply.text == (
        "Your claim ending in 8801 with Northwind HMO is being reviewed. "
        "Your balance will be updated once it's processed."
    )
    assert reply.data["facts"]["claims"] == [
        {"claim_id": "CLM-8801", "status": "in_review", "insurance_plan": "Northwind HMO"}
    ]


async def test_payment_plan_status(billing: BillingAgentClient) -> None:
    reply = await billing.ask("Can I get a payment plan?", patient_id=ONEILL, verified=True)

    assert reply.text == "You're already on a payment plan. Your remaining balance is $59.00."
    assert reply.data["facts"]["enrolled"] is True


async def test_payment_plan_eligibility(billing: BillingAgentClient) -> None:
    reply = await billing.ask("Can I pay this over time?", patient_id=WHITFIELD, verified=True)

    assert "up to 6 monthly payments of about $30.90 for your $185.40 balance" in reply.text


async def test_invoice_breakdown(billing: BillingAgentClient) -> None:
    reply = await billing.ask("Can you break down my bill?", patient_id=WHITFIELD, verified=True)

    assert reply.text.startswith("Follow-up visit on October 12: billed $210.00")
    assert "Echocardiogram on September 1: billed $950.00, insurance paid $764.60" in reply.text


async def test_card_number_is_refused_and_masked(
    billing: BillingAgentClient, app: Starlette
) -> None:
    reply = await billing.ask(
        "Can I just give you my card? It's 4111-1111-1111-1111, expires 04/29",
        patient_id=WHITFIELD,
        verified=True,
    )

    assert reply.text.startswith("For your security, please don't read card numbers")
    assert "secure payment link to your phone ending in 1002" in reply.text
    assert "4111" not in reply.text
    assert reply.data["card_number_redacted"] is True
    assert reply.data["state"] == "input_required"
    assert app.state.ledger.sent_links == ()


async def test_spanish_reply(billing: BillingAgentClient) -> None:
    reply = await billing.ask("¿Cuánto debo?", patient_id=WHITFIELD, verified=True, language="es")

    assert reply.text.startswith(
        "Su saldo actual es de $185.40, por su ecocardiograma del 1 de septiembre."
    )
    assert reply.data["language"] == "es"


async def test_spanish_identity_check(billing: BillingAgentClient) -> None:
    reply = await billing.ask("¿Cuál es mi saldo?", patient_id=None, verified=False, language="es")

    assert reply.text == "Necesito verificar su identidad antes de hablar de su cuenta."


async def test_context_is_reused_to_confirm_a_payment_link(
    billing: BillingAgentClient, app: Starlette
) -> None:
    offer = await billing.ask("I'd like to pay my bill", patient_id=WHITFIELD, verified=True)
    assert offer.data["state"] == "input_required"
    assert offer.text.endswith("Should I send it?")

    sent = await billing.ask(
        "Yes, please", patient_id=WHITFIELD, verified=True, context_id=offer.context_id
    )

    assert sent.context_id == offer.context_id
    assert sent.data["task_id"] == offer.data["task_id"]  # the input-required task resumed
    assert sent.data["state"] == "completed"
    assert sent.text.startswith(
        "Done. I've texted a secure payment link to your phone ending in 1002"
    )
    (link,) = app.state.ledger.sent_links
    assert (link.patient_id, link.amount) == (WHITFIELD, 185.40)


async def test_offer_from_a_completed_answer_carries_to_the_next_turn(
    billing: BillingAgentClient, app: Starlette
) -> None:
    balance = await billing.ask("What's my balance?", patient_id=WHITFIELD, verified=True)
    sent = await billing.ask(
        "Sure", patient_id=WHITFIELD, verified=True, context_id=balance.context_id
    )

    assert sent.data["task_id"] != balance.data["task_id"]  # new task, same conversation
    assert sent.data["intent"] == "confirm_payment_link"
    assert len(app.state.ledger.sent_links) == 1


async def test_yes_without_an_offer_sends_nothing(
    billing: BillingAgentClient, app: Starlette
) -> None:
    reply = await billing.ask("Yes", patient_id=WHITFIELD, verified=True)

    assert reply.data["state"] == "input_required"  # asks what the caller needs
    assert app.state.ledger.sent_links == ()


async def test_conversation_state_does_not_leak_across_patients(
    billing: BillingAgentClient, app: Starlette
) -> None:
    offer = await billing.ask("What's my balance?", patient_id=WHITFIELD, verified=True)
    other = await billing.ask("Yes", patient_id=RAHMAN, verified=True, context_id=offer.context_id)

    assert other.data["intent"] == "help"
    assert app.state.ledger.sent_links == ()


async def test_close_context_cancels_a_pending_question(
    billing: BillingAgentClient, http: httpx.AsyncClient
) -> None:
    offer = await billing.ask("I'd like to pay my bill", patient_id=WHITFIELD, verified=True)

    await billing.close_context(offer.context_id)

    response = await http.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": "1",
            "method": "GetTask",
            "params": {"id": offer.data["task_id"]},
        },
        headers={"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"},
    )
    assert response.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
