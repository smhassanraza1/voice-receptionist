"""Input types for the MCP tools.

Each type pairs a structural `Field` constraint (published in the tool's JSON schema, so the SDK
rejects malformed input before a tool body runs) with a semantic validator that normalises the
value. Validators raise `ValueError` with a short message the calling LLM can act on and never
echo the rejected value back.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, Field

from receptionist.common import clock, fixtures

MAX_AGE_YEARS = 120
MAX_REASON_CHARS = 200

_NAME_PART = r"[^\W\d_]+"  # one run of Unicode letters
_NAME_RE = re.compile(rf"{_NAME_PART}(?:[ '\-]{_NAME_PART})*")
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"})
_PHONE_PUNCTUATION = re.compile(r"[\s().\-]")


def strip_control_chars(text: str) -> str:
    """Drop control/format characters (line breaks become spaces) and collapse whitespace."""
    kept = (
        " " if ch in "\t\r\n" else ch
        for ch in text
        if ch in "\t\r\n" or not unicodedata.category(ch).startswith("C")
    )
    return " ".join("".join(kept).split())


def normalize_name(value: str) -> str:
    name = " ".join(unicodedata.normalize("NFC", value).translate(_APOSTROPHES).split())
    if not _NAME_RE.fullmatch(name):
        raise ValueError("names may only contain letters, spaces, hyphens and apostrophes")
    if len(name.split()) < 2:
        raise ValueError("please provide the patient's first and last name")
    return name


def name_key(name: str) -> str:
    """Accent-, case- and punctuation-insensitive key used to match patient names."""
    decomposed = unicodedata.normalize("NFKD", name.translate(_APOSTROPHES))
    letters = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W\d_]+", letters.casefold().replace("'", "")))


def normalize_phone(value: str) -> str:
    """Return the number in E.164 form. Ten-digit numbers are assumed to be US/Canada (+1)."""
    raw = _PHONE_PUNCTUATION.sub("", value)
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if raw.startswith("+"):
        digits = raw[1:]
    elif len(raw) == 10:
        digits = "1" + raw
    elif len(raw) == 11 and raw.startswith("1"):
        digits = raw
    else:
        raise ValueError("phone number should be a 10-digit US number or start with +country code")
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise ValueError("that does not look like a valid phone number")
    if digits.startswith("1") and (len(digits) != 11 or digits[1] in "01"):
        raise ValueError("US phone numbers have 10 digits and an area code starting with 2-9")
    return "+" + digits


def validate_date_of_birth(value: str) -> str:
    dob = _parse_date(value)
    today = clock.now().date()
    if dob >= today:
        raise ValueError("date of birth must be in the past")
    if today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day)) > MAX_AGE_YEARS:
        raise ValueError(f"date of birth implies an age over {MAX_AGE_YEARS}; please re-check it")
    return dob.isoformat()


def validate_calendar_date(value: str) -> str:
    return _parse_date(value).isoformat()


def clean_reason(value: str) -> str:
    reason = strip_control_chars(value)
    if len(reason) < 2:
        raise ValueError("please give a short reason for the visit")
    return reason[:MAX_REASON_CHARS]


def normalize_confirmation_code(value: object) -> object:
    """Accept 'lkc 7q2m' or 'LKC7Q2M' as spoken/transcribed; the pattern check runs after."""
    if not isinstance(value, str):
        return value
    compact = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return f"LKC-{compact[3:]}" if compact.startswith("LKC") else value


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a real calendar date (use YYYY-MM-DD)") from None


_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"

PatientName = Annotated[
    str,
    Field(min_length=3, max_length=80, description="Patient's full name, e.g. 'Maria Gonzalez'."),
    AfterValidator(normalize_name),
]
Phone = Annotated[
    str,
    Field(
        min_length=7,
        max_length=25,
        description="Phone number; US numbers may omit +1, e.g. '555-010-1001'.",
    ),
    AfterValidator(normalize_phone),
]
DateOfBirth = Annotated[
    str,
    Field(pattern=_ISO_DATE, description="Date of birth as YYYY-MM-DD."),
    AfterValidator(validate_date_of_birth),
]
CalendarDate = Annotated[
    str,
    Field(pattern=_ISO_DATE, description="Local clinic date as YYYY-MM-DD."),
    AfterValidator(validate_calendar_date),
]
Reason = Annotated[
    str,
    Field(min_length=2, max_length=MAX_REASON_CHARS, description="Short reason for the visit."),
    AfterValidator(clean_reason),
]
CancelReason = Annotated[
    str,
    Field(max_length=MAX_REASON_CHARS, description="Optional reason for cancelling."),
    AfterValidator(strip_control_chars),
]
ConfirmationCode = Annotated[
    str,
    Field(
        pattern=r"^LKC-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4}$",
        description="Confirmation code like 'LKC-7Q2M'.",
    ),
    BeforeValidator(normalize_confirmation_code),  # listed last so it runs first
]
SlotId = Annotated[
    str,
    Field(
        pattern=r"^SL-PR\d{2}-\d{8}-\d{4}$",
        description="Slot id exactly as returned by find_available_slots.",
    ),
]
ProviderId = Annotated[str, Field(pattern=r"^PR-\d{2}$", description="Provider id, e.g. 'PR-01'.")]
LanguageCode = Annotated[
    str,
    Field(pattern=r"^[a-z]{2}$", description="ISO 639-1 language code, e.g. 'es' for Spanish."),
]
# Built from the fixtures so the published enum can never drift from the provider roster.
Specialty = Literal[tuple(fixtures.SPECIALTIES)]  # type: ignore[valid-type]
TimeOfDay = Literal["morning", "afternoon", "any"]
