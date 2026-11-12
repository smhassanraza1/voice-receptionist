import pytest
from pydantic import TypeAdapter, ValidationError

from receptionist.mcp_server.validation import (
    ConfirmationCode,
    DateOfBirth,
    PatientName,
    Phone,
    Reason,
    name_key,
    strip_control_chars,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("555-010-1001", "+15550101001"),
        ("(555) 010 1001", "+15550101001"),
        ("+1 555.010.1001", "+15550101001"),
        ("1-555-010-1001", "+15550101001"),
        ("+44 20 7946 0958", "+442079460958"),
        ("0044 20 7946 0958", "+442079460958"),
    ],
)
def test_phone_numbers_are_normalised_to_e164(raw: str, expected: str) -> None:
    assert TypeAdapter(Phone).validate_python(raw) == expected


@pytest.mark.parametrize(
    "raw", ["555-010-100", "+1 055 010 1001", "+1 555 010 10012", "call me maybe", "+0 1234 5678"]
)
def test_invalid_phone_numbers_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Phone).validate_python(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Maria   Gonzalez ", "Maria Gonzalez"),
        ("Margaret O’Neill", "Margaret O'Neill"),
        ("José Álvarez-O'Brien", "José Álvarez-O'Brien"),
        ("Zoë Saint-Clair", "Zoë Saint-Clair"),
        ("李 小龙", "李 小龙"),
    ],
)
def test_names_accept_unicode_letters_hyphens_and_apostrophes(raw: str, expected: str) -> None:
    assert TypeAdapter(PatientName).validate_python(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["Madonna", "R2D2 Smith", "Robert'); DROP TABLE patients", "Ann <b>Lee</b>", "Ann  -  Lee"],
)
def test_invalid_names_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(PatientName).validate_python(raw)


def test_name_key_ignores_case_accents_and_punctuation() -> None:
    assert name_key("Margaret O’Neill") == name_key("MARGARET ONEILL") == "margaret oneill"
    assert name_key("José Álvarez-O'Brien") == name_key("Jose Alvarez Obrien")


def test_date_of_birth_must_be_a_real_past_date_within_120_years() -> None:
    adapter = TypeAdapter(DateOfBirth)
    assert adapter.validate_python("1985-03-14") == "1985-03-14"
    for bad in ["2026-11-03", "1900-01-01", "1985-02-30", "14/03/1985"]:
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)


def test_reason_is_cleaned_of_control_characters() -> None:
    assert TypeAdapter(Reason).validate_python("chest\x07 pain\n\tfollow-up​") == (
        "chest pain follow-up"
    )
    with pytest.raises(ValidationError):
        TypeAdapter(Reason).validate_python("x" * 201)


def test_strip_control_chars_collapses_whitespace() -> None:
    assert strip_control_chars("a\x1b[31m b\r\n\x00c") == "a[31m b c"


@pytest.mark.parametrize("raw", ["LKC-7Q2M", "lkc-7q2m", "LKC 7Q2M", "lkc7q2m"])
def test_confirmation_codes_are_normalised(raw: str) -> None:
    assert TypeAdapter(ConfirmationCode).validate_python(raw) == "LKC-7Q2M"


@pytest.mark.parametrize("raw", ["LKC-7Q2", "LKC-7Q2O", "ABC-7Q2M"])  # O is not in the alphabet
def test_malformed_confirmation_codes_are_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ConfirmationCode).validate_python(raw)
