"""Deterministic engine: keyword intent detection plus templated, speakable replies.

Runs whenever no LLM is configured (always in tests) and is the LLM engine's fallback.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from enum import StrEnum
from typing import Any

from receptionist.billing_agent.engine import AgentReply, BillingRequest
from receptionist.billing_agent.ledger import (
    PAYMENT_LINK_TTL,
    PLAN_MAX_MONTHS,
    PLAN_MIN_BALANCE,
    BillingError,
    BillingLedger,
)
from receptionist.billing_agent.sessions import Session
from receptionist.billing_agent.speech import (
    join_list,
    money,
    normalize_language,
    say,
    service_name,
    spoken_date,
)
from receptionist.billing_agent.tools import BillingTools
from receptionist.common import clock


class Intent(StrEnum):
    BALANCE = "balance"
    INVOICES = "invoices"
    CLAIMS = "claim_status"
    PAYMENT_PLAN = "payment_plan"
    PAY = "pay"
    INSURANCE = "accepted_insurance"
    CONFIRM = "confirm_payment_link"
    DECLINE = "decline_payment_link"
    CARD_NUMBER = "card_number"
    HELP = "help"


ACCOUNT_INTENTS = frozenset(
    {
        Intent.BALANCE,
        Intent.INVOICES,
        Intent.CLAIMS,
        Intent.PAYMENT_PLAN,
        Intent.PAY,
        Intent.CONFIRM,
    }
)


# "Do you take Medicare?" names a plan without saying "insurance".
_PLAN_BRANDS = "|".join(
    re.escape(plan.split()[0].lower()) for plan in BillingLedger.accepted_insurance()
)

# Checked in order against accent-folded, lower-cased text; the first match wins.
_INTENT_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (
        Intent.PAYMENT_PLAN,
        re.compile(
            r"payment plan|installment|monthly payment|over time"
            r"|plan de pagos?|cuotas|plazos|pagos mensuales"
        ),
    ),
    (
        Intent.CLAIMS,
        re.compile(
            r"\bclaims?\b|did (my )?insurance|insurance (pay|paid|cover|process)"
            r"|reclamos?\b|reclamacion|el seguro (pago|cubrio)"
        ),
    ),
    (
        Intent.PAY,
        re.compile(
            r"\bpay\b|payment link|make a payment|credit card|debit card|card number"
            r"|\bpagar\b|enlace de pago|tarjeta|hacer un pago"
        ),
    ),
    (
        Intent.INSURANCE,
        re.compile(
            rf"accept|do you take|in[- ]network|insurance|\baceptan?\b|seguros?\b|{_PLAN_BRANDS}"
        ),
    ),
    (
        Intent.INVOICES,
        re.compile(
            r"invoices?|break ?down|itemi[sz]ed|charges?|statement|older|what (is|was) (it|that) for"
            r"|facturas?|desglose|cargos?|detalle"
        ),
    ),
    (
        Intent.BALANCE,
        re.compile(r"balance|\bowe\b|\bbill\b|amount due|how much|\bsaldo\b|\bdebo\b|cuanto"),
    ),
)
_CONFIRM = re.compile(
    r"^(yes|yeah|yep|sure|ok|okay|please|go ahead|send it|do it"
    r"|si|claro|por favor|de acuerdo|envielo|mandelo|adelante)\b"
)
_DECLINE = re.compile(r"^(no|nope|not now|don'?t|ahora no)\b")


def _fold(text: str) -> str:
    """Lower-case, strip accents and leading punctuation ("¿Sí?" -> "si?")."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"^\W+", "", folded)


def detect_intent(text: str, *, awaiting_confirmation: bool = False) -> Intent:
    folded = _fold(text)
    if awaiting_confirmation:
        if _CONFIRM.search(folded):
            return Intent.CONFIRM
        if _DECLINE.search(folded):
            return Intent.DECLINE
    return next(
        (intent for intent, pattern in _INTENT_PATTERNS if pattern.search(folded)), Intent.HELP
    )


class RuleBasedEngine:
    name = "rules"

    def __init__(self, ledger: BillingLedger) -> None:
        self._ledger = ledger

    async def respond(self, request: BillingRequest, session: Session) -> AgentReply:
        if request.card_number_detected:
            intent = Intent.CARD_NUMBER
        else:
            intent = detect_intent(
                request.question, awaiting_confirmation=session.payment_link_offered
            )
        # An offer only stands for the very next turn.
        session.payment_link_offered = False

        turn = _Turn(BillingTools(self._ledger, request.caller), request, intent, session)
        try:
            return turn.run()
        except BillingError as exc:
            if exc.code != "account_not_found":
                raise
            return turn.reply(say(turn.lang, "no_account"))


class _Turn:
    """Builds the reply for one detected intent."""

    def __init__(
        self, tools: BillingTools, request: BillingRequest, intent: Intent, session: Session
    ) -> None:
        self.tools = tools
        self.caller = request.caller
        self.question = request.question
        self.lang = normalize_language(request.caller.language)
        self.intent = intent
        self.session = session
        self.today = clock.now().date()

    def reply(
        self,
        text: str,
        *,
        needs_input: bool = False,
        needs_verification: bool = False,
        facts: dict[str, Any] | None = None,
    ) -> AgentReply:
        return AgentReply(
            text=text,
            intent=str(self.intent),
            engine=RuleBasedEngine.name,
            language=self.lang,
            needs_input=needs_input,
            needs_verification=needs_verification,
            facts=facts or {},
        )

    def offer_payment_link(self) -> None:
        self.session.payment_link_offered = True

    def run(self) -> AgentReply:
        if self.intent in ACCOUNT_INTENTS and not self.caller.can_access_account:
            return self.unverified()
        match self.intent:
            case Intent.BALANCE:
                return self.balance()
            case Intent.INVOICES:
                return self.invoices()
            case Intent.CLAIMS:
                return self.claims()
            case Intent.PAYMENT_PLAN:
                return self.payment_plan()
            case Intent.PAY:
                return self.pay()
            case Intent.CONFIRM:
                return self.send_link()
            case Intent.DECLINE:
                return self.reply(say(self.lang, "link_declined"))
            case Intent.CARD_NUMBER:
                return self.card_number()
            case Intent.INSURANCE:
                return self.insurance()
            case _:
                return self.reply(say(self.lang, "help"), needs_input=True)

    def _date(self, day: date) -> str:
        return spoken_date(day, self.lang, today=self.today)

    def balance(self) -> AgentReply:
        summary = self.tools.account_summary()
        facts = summary.model_dump(mode="json")
        open_items = [line for line in summary.invoices if line.balance > 0]
        if not open_items:
            return self.reply(say(self.lang, "balance_none"), facts=facts)
        amount = money(summary.balance_due)
        if len(open_items) == 1:
            line = open_items[0]
            text = say(
                self.lang,
                "balance_one",
                amount=amount,
                service=service_name(line.description, self.lang),
                date=self._date(line.service_date),
            )
        else:
            items = [
                say(
                    self.lang,
                    "balance_item",
                    amount=money(line.balance),
                    service=service_name(line.description, self.lang),
                    date=self._date(line.service_date),
                )
                for line in open_items
            ]
            text = say(
                self.lang,
                "balance_many",
                amount=amount,
                count=len(items),
                items=join_list(items, self.lang),
            )
        if summary.on_payment_plan:
            text += say(self.lang, "balance_on_plan")
        else:
            text += say(self.lang, "offer_link")
            self.offer_payment_link()
        return self.reply(text, facts=facts)

    def invoices(self) -> AgentReply:
        summary = self.tools.account_summary()
        facts = summary.model_dump(mode="json")
        if not summary.invoices:
            return self.reply(say(self.lang, "invoices_none"), facts=facts)
        sentences = []
        for line in summary.invoices:
            service = service_name(line.description, self.lang)
            remaining = (
                say(self.lang, "remaining", amount=money(line.balance))
                if line.balance > 0
                else say(self.lang, "paid_in_full")
            )
            sentences.append(
                say(
                    self.lang,
                    "invoice_item",
                    service=service[:1].upper() + service[1:],
                    date=self._date(line.service_date),
                    billed=money(line.amount_billed),
                    insurance=money(line.insurance_paid),
                    patient=money(line.patient_paid),
                    remaining=remaining,
                )
            )
        return self.reply(" ".join(sentences), facts=facts)

    def claims(self) -> AgentReply:
        claims = self.tools.claims()
        facts = {"claims": [claim.model_dump(mode="json") for claim in claims]}
        if not claims:
            return self.reply(say(self.lang, "claims_none"), facts=facts)
        text = " ".join(
            say(
                self.lang,
                "claim_item",
                last4=claim.claim_id[-4:],
                plan=claim.insurance_plan or say(self.lang, "your_insurance"),
                status=say(self.lang, f"claim_{claim.status}"),
            )
            for claim in claims
        )
        if any(claim.status in ("submitted", "in_review") for claim in claims):
            text += say(self.lang, "claim_pending_note")
        return self.reply(text, facts=facts)

    def payment_plan(self) -> AgentReply:
        plan = self.tools.payment_plan()
        facts = plan.model_dump(mode="json")
        balance = money(plan.balance_due)
        if plan.enrolled:
            text = say(self.lang, "plan_enrolled", balance=balance)
        elif plan.eligible:
            text = say(
                self.lang,
                "plan_eligible",
                months=plan.max_months,
                monthly=money(plan.estimated_monthly or 0),
                balance=balance,
            )
            self.offer_payment_link()
        elif plan.balance_due <= 0:
            text = say(self.lang, "plan_no_balance")
        else:
            text = say(
                self.lang, "plan_too_small", minimum=money(plan.minimum_balance), balance=balance
            )
            self.offer_payment_link()
        return self.reply(text, facts=facts)

    def pay(self) -> AgentReply:
        summary = self.tools.account_summary()
        if summary.balance_due <= 0:
            return self.reply(say(self.lang, "pay_none"))
        self.offer_payment_link()
        text = say(
            self.lang,
            "pay_offer",
            balance=money(summary.balance_due),
            last4=self.tools.phone_last4(),
        )
        return self.reply(text, needs_input=True, facts={"balance_due": summary.balance_due})

    def send_link(self) -> AgentReply:
        try:
            link = self.tools.send_payment_link()
        except BillingError as exc:
            if exc.code != "no_balance_due":
                raise
            return self.reply(say(self.lang, "pay_none"))
        hours = int(PAYMENT_LINK_TTL.total_seconds() // 3600)
        return self.reply(
            say(self.lang, "link_sent", last4=link.phone_last4, hours=hours),
            facts={"payment_link": link.model_dump(mode="json")},
        )

    def unverified(self) -> AgentReply:
        """General information only, plus the reason account details are withheld."""
        general = {
            Intent.PAYMENT_PLAN: say(
                self.lang, "plan_policy", minimum=money(PLAN_MIN_BALANCE), months=PLAN_MAX_MONTHS
            ),
            Intent.PAY: say(self.lang, "pay_policy"),
        }.get(self.intent)
        verify = say(self.lang, "verify_needed")
        text = f"{general} {verify}" if general else verify
        return self.reply(text, needs_verification=True)

    def card_number(self) -> AgentReply:
        text = say(self.lang, "card_refusal")
        if not self.caller.can_access_account:
            return self.reply(text + say(self.lang, "card_unverified"), needs_verification=True)
        if self.tools.account_summary().balance_due <= 0:
            return self.reply(text + say(self.lang, "card_no_balance"))
        self.offer_payment_link()
        text += say(self.lang, "card_offer", last4=self.tools.phone_last4())
        return self.reply(text, needs_input=True)

    def insurance(self) -> AgentReply:
        plans = self.tools.accepted_insurance()
        facts = {"plans": list(plans)}
        named = BillingLedger.find_accepted_plan(_fold(self.question))
        if named:
            return self.reply(say(self.lang, "insurance_yes", plan=named), facts=facts)
        return self.reply(
            say(self.lang, "insurance_list", plans=join_list(plans, self.lang)), facts=facts
        )
