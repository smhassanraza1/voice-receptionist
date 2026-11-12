"""Unit tests for the pure building blocks: redaction, phrasing, intents, sessions, ledger."""

from __future__ import annotations

from datetime import date

import pytest

from receptionist.billing_agent.ledger import BillingError, BillingLedger
from receptionist.billing_agent.rules import Intent, detect_intent
from receptionist.billing_agent.security import redact_card_numbers
from receptionist.billing_agent.sessions import SessionStore
from receptionist.billing_agent.speech import join_list, money, normalize_language, spoken_date


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4111111111111111", "****************"),
        (
            "card 4111 1111 1111 1111 exp 12/29 cvv 123",
            "card **** **** **** **** exp **/** cvv ***",
        ),
        ("amex 3782-822463-10005", "amex ****-******-*****"),
    ],
)
def test_card_numbers_are_masked(text: str, expected: str) -> None:
    assert redact_card_numbers(text) == (expected, True)


@pytest.mark.parametrize(
    "text", ["my balance from 10/12", "call me at 555 010 1002", "claim CLM-8801", "P-1002"]
)
def test_ordinary_numbers_are_left_alone(text: str) -> None:
    assert redact_card_numbers(text) == (text, False)


def test_speech_formatting() -> None:
    today = date(2026, 11, 2)
    assert money(185.4) == "$185.40"
    assert money(1234.5) == "$1,234.50"
    assert spoken_date(date(2026, 9, 1), "en", today=today) == "September 1"
    assert spoken_date(date(2025, 12, 24), "en", today=today) == "December 24, 2025"
    assert spoken_date(date(2026, 9, 1), "es", today=today) == "1 de septiembre"
    assert join_list(["A", "B", "C"], "en") == "A, B, and C"
    assert join_list(["A", "B", "C"], "es") == "A, B y C"
    assert normalize_language("es-MX") == "es"
    assert normalize_language("ja") == "en"  # rule-based replies fall back to English


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("How much do I owe?", Intent.BALANCE),
        ("I have a question about my bill", Intent.BALANCE),
        ("What were the charges for?", Intent.INVOICES),
        ("and what about the older one?", Intent.INVOICES),
        ("Did my insurance pay for the visit?", Intent.CLAIMS),
        ("Did Medicare pay my claim yet?", Intent.CLAIMS),
        ("Can I pay in installments?", Intent.PAYMENT_PLAN),
        ("I want to pay with my credit card", Intent.PAY),
        ("Do you take BlueHarbor?", Intent.INSURANCE),
        ("What insurance do you accept?", Intent.INSURANCE),
        ("¿Cuál es mi saldo?", Intent.BALANCE),
        ("¿Ya pagó el seguro mi reclamación?", Intent.CLAIMS),
        ("¿Puedo hacer un plan de pagos?", Intent.PAYMENT_PLAN),
        ("¿Aceptan Medicare?", Intent.INSURANCE),
        ("Good morning", Intent.HELP),
    ],
)
def test_intent_detection(question: str, intent: Intent) -> None:
    assert detect_intent(question) is intent


def test_confirmation_only_counts_after_an_offer() -> None:
    assert detect_intent("Yes please") is Intent.HELP
    assert detect_intent("Yes please", awaiting_confirmation=True) is Intent.CONFIRM
    assert detect_intent("Sí, por favor", awaiting_confirmation=True) is Intent.CONFIRM
    assert detect_intent("No thanks", awaiting_confirmation=True) is Intent.DECLINE


def test_session_store_expires_and_bounds_sessions() -> None:
    now = 0.0
    store = SessionStore(ttl_s=60, max_sessions=2, max_turns=2, clock=lambda: now)

    first = store.get("a", "P-1002")
    assert store.get("a", "P-1002") is first

    now = 61.0
    assert store.get("a", "P-1002") is not first  # expired

    store.get("b", None)
    store.get("c", None)
    assert len(store) == 2  # least recently used ("a") evicted


def test_session_resets_when_the_account_changes() -> None:
    store = SessionStore(ttl_s=60, max_sessions=10, max_turns=2)
    session = store.get("call-1", None)
    session.payment_link_offered = True

    assert store.get("call-1", "P-1002") is not session


def test_session_keeps_only_recent_turns() -> None:
    session = SessionStore(ttl_s=60, max_sessions=1, max_turns=2).get("call", None)
    for n in range(3):
        session.add_turn([{"role": "user", "content": str(n)}])

    assert [m["content"] for m in session.history()] == ["1", "2"]


def test_ledger_resolves_dates_against_the_clinic_clock() -> None:
    summary = BillingLedger().account_summary("P-1002")

    assert summary.balance_due == 185.40
    assert [(line.invoice_id, line.service_date) for line in summary.invoices] == [
        ("INV-5003", date(2026, 10, 12)),
        ("INV-5002", date(2026, 9, 1)),
    ]


def test_payment_link_requires_a_balance() -> None:
    ledger = BillingLedger()

    with pytest.raises(BillingError) as excinfo:
        ledger.send_payment_link("P-1001")  # paid in full

    assert excinfo.value.code == "no_balance_due"
    assert ledger.sent_links == ()
