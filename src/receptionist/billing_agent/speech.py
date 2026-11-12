"""Speakable phrasing for billing replies (English and Spanish).

Everything here produces text a TTS voice reads aloud: no markdown, amounts as "$185.40",
dates as "September 1" / "1 de septiembre".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

SUPPORTED_LANGUAGES = ("en", "es")

# Spelled out rather than strftime("%B") so output never depends on the process locale.
_MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTHS_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)

# Invoice descriptions from the fixtures, as a Spanish speaker would hear them.
_SERVICES_ES = {
    "office visit": "consulta",
    "echocardiogram": "ecocardiograma",
    "follow-up visit": "visita de seguimiento",
    "new patient visit": "consulta de paciente nuevo",
    "biopsy": "biopsia",
}

PHRASES: dict[str, dict[str, str]] = {
    "en": {
        "verify_needed": "I'll need to verify your identity before I can discuss your account.",
        "no_account": "I couldn't find a billing account for this patient. "
        "Our billing team can look into it.",
        "balance_none": "Good news: you don't have a balance right now. Everything is paid in full.",
        "balance_one": "Your current balance is {amount}, for your {service} on {date}.",
        "balance_many": "Your current balance is {amount}, across {count} visits: {items}.",
        "balance_item": "{amount} for your {service} on {date}",
        "balance_on_plan": " That balance is on your payment plan.",
        "offer_link": " If you'd like, I can text you a secure payment link.",
        "invoices_none": "I don't see any invoices on your account.",
        "invoice_item": "{service} on {date}: billed {billed}, insurance paid {insurance}, "
        "you paid {patient}, {remaining}.",
        "paid_in_full": "paid in full",
        "remaining": "{amount} remaining",
        "claims_none": "I don't see any open insurance claims on your account.",
        "claim_item": "Your claim ending in {last4} with {plan} {status}.",
        "claim_submitted": "has been submitted and is waiting for review",
        "claim_in_review": "is being reviewed",
        "claim_approved": "was approved",
        "claim_denied": "was denied. Our billing team can help you with an appeal",
        "claim_pending_note": " Your balance will be updated once it's processed.",
        "your_insurance": "your insurance",
        "plan_enrolled": "You're already on a payment plan. Your remaining balance is {balance}.",
        "plan_eligible": "You qualify for a payment plan of up to {months} monthly payments of "
        "about {monthly} for your {balance} balance. "
        "I can text you a secure link to set it up.",
        "plan_no_balance": "You don't have a balance right now, so there's nothing to put on "
        "a payment plan.",
        "plan_too_small": "Payment plans are for balances of {minimum} or more, and your balance "
        "is {balance}. I can text you a secure link to pay it in full.",
        "plan_policy": "We offer payment plans for balances of {minimum} or more, "
        "with up to {months} monthly payments.",
        "insurance_list": "We accept {plans}.",
        "insurance_yes": "Yes, we accept {plan}.",
        "pay_offer": "Your balance is {balance}. For your security I can't take card details "
        "over the phone, but I can text a secure payment link to your phone ending in {last4}. "
        "Should I send it?",
        "pay_none": "You don't have a balance to pay right now.",
        "pay_policy": "You can pay securely with a payment link we text to the phone number "
        "on file. We never take card numbers over the phone.",
        "link_sent": "Done. I've texted a secure payment link to your phone ending in {last4}. "
        "It's good for {hours} hours.",
        "link_declined": "No problem, I won't send it.",
        "card_refusal": "For your security, please don't read card numbers over the phone. "
        "I haven't kept it.",
        "card_offer": " I can text a secure payment link to your phone ending in {last4} instead. "
        "Should I send it?",
        "card_unverified": " Once your identity is verified, I can text you a secure payment link.",
        "card_no_balance": " You also don't have a balance due right now.",
        "help": "I can help with your balance, invoices, insurance claims, payment plans, "
        "or which insurance we accept. What would you like to know?",
        "error": "Sorry, I ran into a problem checking your billing information. "
        "Please try again in a moment.",
        "unavailable": "I'm sorry, our billing system isn't available right now. "
        "Our billing team can call you back, or you can try again a little later.",
        "and": "and",
    },
    "es": {
        "verify_needed": "Necesito verificar su identidad antes de hablar de su cuenta.",
        "no_account": "No encontré una cuenta de facturación para este paciente. "
        "Nuestro equipo de facturación puede revisarlo.",
        "balance_none": "Buenas noticias: no tiene saldo pendiente. Todo está pagado.",
        "balance_one": "Su saldo actual es de {amount}, por su {service} del {date}.",
        "balance_many": "Su saldo actual es de {amount}, de {count} visitas: {items}.",
        "balance_item": "{amount} por su {service} del {date}",
        "balance_on_plan": " Ese saldo está en su plan de pagos.",
        "offer_link": " Si lo desea, puedo enviarle un enlace de pago seguro por mensaje de texto.",
        "invoices_none": "No veo facturas en su cuenta.",
        "invoice_item": "{service} del {date}: se facturó {billed}, el seguro pagó {insurance}, "
        "usted pagó {patient}, {remaining}.",
        "paid_in_full": "está pagado",
        "remaining": "quedan {amount}",
        "claims_none": "No veo reclamos de seguro abiertos en su cuenta.",
        "claim_item": "Su reclamo que termina en {last4} con {plan} {status}.",
        "claim_submitted": "fue enviado y está en espera de revisión",
        "claim_in_review": "está en revisión",
        "claim_approved": "fue aprobado",
        "claim_denied": "fue rechazado. Nuestro equipo de facturación puede ayudarle a apelar",
        "claim_pending_note": " Actualizaremos su saldo cuando se procese.",
        "your_insurance": "su seguro",
        "plan_enrolled": "Ya tiene un plan de pagos. Su saldo restante es de {balance}.",
        "plan_eligible": "Califica para un plan de hasta {months} pagos mensuales de "
        "aproximadamente {monthly} para su saldo de {balance}. "
        "Puedo enviarle un enlace seguro por mensaje de texto para configurarlo.",
        "plan_no_balance": "No tiene saldo pendiente, así que no necesita un plan de pagos.",
        "plan_too_small": "Los planes de pago son para saldos de {minimum} o más, y su saldo es "
        "de {balance}. Puedo enviarle un enlace seguro para pagarlo completo.",
        "plan_policy": "Ofrecemos planes de pago para saldos de {minimum} o más, "
        "con hasta {months} pagos mensuales.",
        "insurance_list": "Aceptamos {plans}.",
        "insurance_yes": "Sí, aceptamos {plan}.",
        "pay_offer": "Su saldo es de {balance}. Por su seguridad, no puedo tomar datos de "
        "tarjeta por teléfono, pero puedo enviarle un enlace de pago seguro al teléfono que "
        "termina en {last4}. ¿Se lo envío?",
        "pay_none": "No tiene saldo pendiente por pagar.",
        "pay_policy": "Puede pagar de forma segura con un enlace que le enviamos por mensaje de "
        "texto al teléfono registrado. Nunca tomamos números de tarjeta por teléfono.",
        "link_sent": "Listo. Le envié un enlace de pago seguro al teléfono que termina en "
        "{last4}. Es válido por {hours} horas.",
        "link_declined": "Está bien, no lo enviaré.",
        "card_refusal": "Por su seguridad, no lea números de tarjeta por teléfono. "
        "No lo he guardado.",
        "card_offer": " Puedo enviarle un enlace de pago seguro al teléfono que termina en "
        "{last4}. ¿Se lo envío?",
        "card_unverified": " Cuando verifiquemos su identidad, puedo enviarle un enlace de "
        "pago seguro.",
        "card_no_balance": " Además, no tiene saldo pendiente en este momento.",
        "help": "Puedo ayudarle con su saldo, sus facturas, reclamos de seguro, planes de pago "
        "o los seguros que aceptamos. ¿Qué le gustaría saber?",
        "error": "Lo siento, tuve un problema al revisar su información de facturación. "
        "Inténtelo de nuevo en un momento.",
        "unavailable": "Lo siento, nuestro sistema de facturación no está disponible en este "
        "momento. Nuestro equipo puede devolverle la llamada, o puede intentarlo más tarde.",
        "and": "y",
    },
}


def normalize_language(language: str | None) -> str:
    """Map an ISO 639-1 code (or a locale like "es-MX") to a supported reply language."""
    code = (language or "").split("-")[0].strip().lower()
    return code if code in SUPPORTED_LANGUAGES else "en"


def say(language: str, key: str, **values: object) -> str:
    return PHRASES[normalize_language(language)][key].format(**values)


def money(amount: float) -> str:
    return f"${amount:,.2f}"


def spoken_date(day: date, language: str, *, today: date) -> str:
    """'September 1' / '1 de septiembre', adding the year only when it isn't this year."""
    if normalize_language(language) == "es":
        text = f"{day.day} de {_MONTHS_ES[day.month - 1]}"
        return text if day.year == today.year else f"{text} de {day.year}"
    text = f"{_MONTHS_EN[day.month - 1]} {day.day}"
    return text if day.year == today.year else f"{text}, {day.year}"


def service_name(description: str, language: str) -> str:
    """Lower-case service name for use mid-sentence ("your echocardiogram")."""
    lowered = description.lower()
    if normalize_language(language) == "es":
        return _SERVICES_ES.get(lowered, lowered)
    return lowered


def join_list(items: Sequence[str], language: str) -> str:
    """'A, B, and C' / 'A, B y C'."""
    conjunction = say(language, "and")
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} {conjunction} {items[1]}"
    serial_comma = "," if normalize_language(language) == "en" else ""
    return f"{', '.join(items[:-1])}{serial_comma} {conjunction} {items[-1]}"
