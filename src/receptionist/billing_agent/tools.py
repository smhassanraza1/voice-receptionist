"""Billing tools, bound to one caller.

Both engines go through this class, so the verification rule lives in exactly one place:
account tools raise `BillingError("identity_not_verified")` unless the receptionist marked the
caller verified, and the LLM is never offered them in that case. The patient id comes from the
A2A request, never from model-generated arguments.
"""

from __future__ import annotations

from typing import Any

from receptionist.billing_agent.engine import Caller
from receptionist.billing_agent.ledger import (
    AccountSummary,
    BillingError,
    BillingLedger,
    Claim,
    PaymentLink,
    PaymentPlanStatus,
)

_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


def _spec(name: str, description: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": _NO_ARGS},
    }


_GENERAL_SPECS = [
    _spec("list_accepted_insurance", "Insurance plans the clinic accepts."),
]
_ACCOUNT_SPECS = [
    _spec(
        "get_account_summary",
        "The caller's balance due, insurance plan, payment plan flag and every invoice "
        "(service date, description, billed, insurance paid, patient paid, balance).",
    ),
    _spec("get_claim_status", "The caller's insurance claims and their status."),
    _spec(
        "get_payment_plan",
        "Whether the caller is on a payment plan or eligible for one, with estimated terms.",
    ),
    _spec(
        "send_payment_link",
        "Text a secure payment link to the caller's phone on file. "
        "Only call this after the caller agreed to receive it.",
    ),
]


class BillingTools:
    def __init__(self, ledger: BillingLedger, caller: Caller) -> None:
        self._ledger = ledger
        self._caller = caller

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI tool definitions available to this caller."""
        if self._caller.can_access_account:
            return [*_GENERAL_SPECS, *_ACCOUNT_SPECS]
        return list(_GENERAL_SPECS)

    def call(self, name: str) -> dict[str, Any]:
        """Run a model-requested tool; failures come back as data the model can explain.

        Every tool is argument-free (the account is bound to the caller), so model-generated
        arguments are deliberately ignored.
        """
        handlers = {
            "list_accepted_insurance": lambda: {"plans": list(self.accepted_insurance())},
            "get_account_summary": self.account_summary,
            "get_claim_status": lambda: {"claims": self.claims()},
            "get_payment_plan": self.payment_plan,
            "send_payment_link": self.send_payment_link,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": "unknown_tool", "message": f"No tool named {name!r}."}
        try:
            return _to_json(handler())
        except BillingError as exc:
            return {"error": exc.code, "message": str(exc)}

    # Typed accessors, used directly by the rule-based engine.

    def accepted_insurance(self) -> tuple[str, ...]:
        return self._ledger.accepted_insurance()

    def account_summary(self) -> AccountSummary:
        return self._ledger.account_summary(self._patient_id())

    def claims(self) -> list[Claim]:
        return self._ledger.claims(self._patient_id())

    def payment_plan(self) -> PaymentPlanStatus:
        return self._ledger.payment_plan(self._patient_id())

    def send_payment_link(self) -> PaymentLink:
        return self._ledger.send_payment_link(self._patient_id())

    def phone_last4(self) -> str | None:
        return self._ledger.phone_last4(self._patient_id())

    def _patient_id(self) -> str:
        patient_id = self._caller.account_key
        if patient_id is None:
            raise BillingError("identity_not_verified", "Caller identity is not verified.")
        return patient_id


def _to_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_json(item) for key, item in value.items()}
    return value
