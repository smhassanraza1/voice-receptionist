"""In-memory billing ledger seeded from `receptionist.common.fixtures`.

Dates are resolved against the clinic clock at query time, so `CLINIC_NOW` pins every
answer for evals.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from receptionist.common import clock
from receptionist.common.fixtures import (
    ACCEPTED_INSURANCE,
    BILLING_ACCOUNTS,
    BillingAccount,
    patient_by_id,
)

PLAN_MIN_BALANCE = 50.00
PLAN_MAX_MONTHS = 6
PLAN_MIN_INSTALLMENT = 25.00
PAYMENT_LINK_TTL = timedelta(hours=24)

ClaimState = Literal["submitted", "in_review", "approved", "denied"]


class BillingError(Exception):
    """A business-rule failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvoiceLine(BaseModel):
    invoice_id: str
    service_date: date
    description: str
    amount_billed: float
    insurance_paid: float
    patient_paid: float
    balance: float


class AccountSummary(BaseModel):
    patient_id: str
    insurance_plan: str | None
    balance_due: float
    on_payment_plan: bool
    invoices: list[InvoiceLine]  # newest first


class Claim(BaseModel):
    claim_id: str
    status: ClaimState
    insurance_plan: str | None


class PaymentPlanStatus(BaseModel):
    enrolled: bool
    eligible: bool
    balance_due: float
    minimum_balance: float = PLAN_MIN_BALANCE
    max_months: int | None = None
    estimated_monthly: float | None = None


class PaymentLink(BaseModel):
    link_id: str
    patient_id: str
    amount: float
    phone_last4: str
    expires_at: datetime


class BillingLedger:
    """Read-mostly view over the seeded billing accounts, plus a log of sent payment links."""

    def __init__(self, accounts: Iterable[BillingAccount] = BILLING_ACCOUNTS) -> None:
        self._accounts = {account.patient_id: account for account in accounts}
        self._sent_links: list[PaymentLink] = []

    @property
    def sent_links(self) -> tuple[PaymentLink, ...]:
        return tuple(self._sent_links)

    def account_summary(self, patient_id: str) -> AccountSummary:
        account = self._account(patient_id)
        today = clock.now().date()
        invoices = sorted(
            (
                InvoiceLine(
                    invoice_id=invoice.invoice_id,
                    service_date=today + timedelta(days=invoice.service_date_offset),
                    description=invoice.description,
                    amount_billed=invoice.amount_billed,
                    insurance_paid=invoice.insurance_paid,
                    patient_paid=invoice.patient_paid,
                    balance=invoice.balance,
                )
                for invoice in account.invoices
            ),
            key=lambda line: line.service_date,
            reverse=True,
        )
        return AccountSummary(
            patient_id=patient_id,
            insurance_plan=account.insurance_plan,
            balance_due=round(sum(line.balance for line in invoices), 2),
            on_payment_plan=account.on_payment_plan,
            invoices=invoices,
        )

    def claims(self, patient_id: str) -> list[Claim]:
        account = self._account(patient_id)
        return [
            Claim(claim_id=claim_id, status=status, insurance_plan=account.insurance_plan)
            for claim_id, status in account.claims.items()
        ]

    def payment_plan(self, patient_id: str) -> PaymentPlanStatus:
        """Enrollment status, or eligibility: $50+ balance, up to 6 payments of at least $25."""
        summary = self.account_summary(patient_id)
        balance = summary.balance_due
        if summary.on_payment_plan:
            return PaymentPlanStatus(enrolled=True, eligible=True, balance_due=balance)
        if balance < PLAN_MIN_BALANCE:
            return PaymentPlanStatus(enrolled=False, eligible=False, balance_due=balance)
        months = min(PLAN_MAX_MONTHS, math.floor(balance / PLAN_MIN_INSTALLMENT))
        return PaymentPlanStatus(
            enrolled=False,
            eligible=True,
            balance_due=balance,
            max_months=months,
            estimated_monthly=round(balance / months, 2),
        )

    def send_payment_link(self, patient_id: str) -> PaymentLink:
        """Text a secure payment link to the phone on file (recorded, not actually sent)."""
        balance = self.account_summary(patient_id).balance_due
        if balance <= 0:
            raise BillingError("no_balance_due", "There is no balance to pay.")
        patient = patient_by_id(patient_id)
        if patient is None:
            raise BillingError("no_phone_on_file", "No phone number on file.")
        link = PaymentLink(
            link_id=f"PL-{secrets.token_hex(4).upper()}",
            patient_id=patient_id,
            amount=balance,
            phone_last4=patient.phone[-4:],
            expires_at=clock.now() + PAYMENT_LINK_TTL,
        )
        self._sent_links.append(link)
        return link

    def phone_last4(self, patient_id: str) -> str | None:
        patient = patient_by_id(patient_id)
        return patient.phone[-4:] if patient else None

    @staticmethod
    def accepted_insurance() -> tuple[str, ...]:
        return ACCEPTED_INSURANCE

    @staticmethod
    def find_accepted_plan(text: str) -> str | None:
        """The accepted plan a caller named, matching on the plan's brand ("Medicare", "BlueHarbor")."""
        lowered = text.lower()
        return next(
            (plan for plan in ACCEPTED_INSURANCE if plan.split()[0].lower() in lowered), None
        )

    def _account(self, patient_id: str) -> BillingAccount:
        try:
            return self._accounts[patient_id]
        except KeyError:
            raise BillingError("account_not_found", "No billing account found.") from None
