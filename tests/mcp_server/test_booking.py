import sqlite3
import threading
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from receptionist.common import fixtures
from receptionist.mcp_server.db import (
    IDENTITY_FAILED_MESSAGE,
    LATE_CANCELLATION_FEE_USD,
    ClinicError,
    ClinicStore,
    IdentityVerificationError,
    SlotUnavailableError,
    reset_database,
)

MARIA = fixtures.PATIENTS[0]  # P-1001, has LKC-7Q2M on Thursday
JAMES = fixtures.PATIENTS[1]  # P-1002, has LKC-4H8T on Tuesday 14:30


def rows(path: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


def book_new_patient(store: ClinicStore, slot_id: str, name: str = "Nora Patel"):
    return store.book(
        slot_id=slot_id,
        patient_name=name,
        date_of_birth="1990-06-15",
        phone="+15550109999",
        reason="sore throat",
    )


# --------------------------------------------------------------------------- seeding


def test_seeded_schedule_respects_hours_workdays_and_lunch(store: ClinicStore) -> None:
    slots = rows(store.path, "SELECT slot_id, provider_id, local_date, local_time FROM slots")
    providers = {p.provider_id: p for p in fixtures.PROVIDERS}
    for _slot_id, provider_id, local_date, local_time in slots:
        weekday = date.fromisoformat(local_date).weekday()
        opens, closes = fixtures.CLINIC["hours"][weekday]
        assert weekday in providers[provider_id].workdays
        assert opens <= local_time < closes
        if weekday < 5:
            assert not "12:00" <= local_time < "13:00"
    saturday = {t for _, p, d, t in slots if p == "PR-03" and d == "2026-11-07"}
    assert {"09:00", "12:00", "12:30"} <= saturday  # no lunch break on Saturdays
    assert "SL-PR02-20261103-1430" in {s[0] for s in slots}


def test_about_a_third_of_slots_are_held_deterministically(store: ClinicStore, tmp_path) -> None:
    held = rows(store.path, "SELECT slot_id FROM slots WHERE held = 1 ORDER BY slot_id")
    total = rows(store.path, "SELECT COUNT(*) FROM slots")[0][0]
    assert 0.30 < len(held) / total < 0.40
    other = tmp_path / "other.db"
    reset_database(other)
    assert rows(other, "SELECT slot_id FROM slots WHERE held = 1 ORDER BY slot_id") == held


def test_seed_appointments_occupy_their_slots(store: ClinicStore) -> None:
    booked = dict(
        rows(
            store.path, "SELECT confirmation_code, slot_id FROM appointments WHERE status='booked'"
        )
    )
    assert booked == {
        "LKC-7Q2M": "SL-PR01-20261105-1000",
        "LKC-4H8T": "SL-PR02-20261103-1430",
        "LKC-9P3K": "SL-PR03-20261104-0930",
        "LKC-2B6D": "SL-PR04-20261105-1100",
    }
    offered = {s.slot_id for s in store.find_slots(provider_id="PR-02", limit=10)}
    assert "SL-PR02-20261103-1430" not in offered


# --------------------------------------------------------------------------- search


def test_slots_start_after_the_booking_lead_time(store: ClinicStore) -> None:
    slots = store.find_slots(date_from="2026-11-02", date_to="2026-11-02", limit=10)
    assert slots and all(s.local_time >= "10:00" for s in slots)  # now is 09:00
    assert [s.start for s in slots] == sorted(s.start for s in slots)


def test_time_of_day_filter(store: ClinicStore) -> None:
    morning = store.find_slots(specialty="dermatology", time_of_day="morning", limit=10)
    afternoon = store.find_slots(specialty="dermatology", time_of_day="afternoon", limit=10)
    assert morning and all(s.local_time < "12:00" for s in morning)
    assert afternoon and all(s.local_time >= "13:00" for s in afternoon)
    assert {s.provider_id for s in morning + afternoon} == {"PR-04"}


def test_search_window_is_limited_to_the_booking_horizon(store: ClinicStore) -> None:
    late = store.find_slots(date_from="2026-11-28", date_to="2027-01-31", limit=10)
    assert late and max(s.local_date for s in late) <= "2026-11-30"
    with pytest.raises(ClinicError, match="past"):
        store.find_slots(date_from="2026-11-01")
    with pytest.raises(ClinicError, match="28 days"):
        store.find_slots(date_from="2026-12-01")
    with pytest.raises(ClinicError, match="end date"):
        store.find_slots(date_from="2026-11-10", date_to="2026-11-09")


def test_provider_filters_are_checked(store: ClinicStore) -> None:
    with pytest.raises(ClinicError, match="no provider"):
        store.find_slots(provider_id="PR-99")
    with pytest.raises(ClinicError, match="cardiology, not pediatrics"):
        store.find_slots(provider_id="PR-02", specialty="pediatrics")


def test_list_providers_filters(store: ClinicStore) -> None:
    assert [p.name for p in store.list_providers(language="ja")] == ["Dr. Kenji Watanabe"]
    assert len(store.list_providers(specialty="family_medicine")) == 2
    assert len(store.list_providers()) == len(fixtures.PROVIDERS)


# --------------------------------------------------------------------------- booking


def test_booking_a_new_patient_creates_a_record(store: ClinicStore) -> None:
    slot = store.find_slots(specialty="family_medicine")[0]
    booking = book_new_patient(store, slot.slot_id)
    assert booking.identity_verified
    assert booking.patient_id == "P-1006"
    assert booking.confirmation_code.startswith("LKC-")
    assert slot.slot_id not in {s.slot_id for s in store.find_slots(specialty="family_medicine")}


def test_existing_patients_are_matched_by_name_dob_and_phone(store: ClinicStore) -> None:
    slot = store.find_slots(provider_id="PR-05")[0]
    booking = store.book(
        slot_id=slot.slot_id,
        patient_name="maria gonzález",
        date_of_birth=MARIA.date_of_birth,
        phone=MARIA.phone,
        reason="follow-up",
    )
    assert (booking.patient_id, booking.identity_verified) == (MARIA.patient_id, True)
    assert "is_new_patient" not in booking.model_dump()


def test_name_and_dob_without_the_phone_on_file_never_reach_the_record(store: ClinicStore) -> None:
    impostor = {"patient_name": MARIA.full_name, "date_of_birth": MARIA.date_of_birth}
    first, second = store.find_slots(provider_id="PR-05", limit=2)
    booking = store.book(
        slot_id=first.slot_id, phone="+15550108888", reason="follow-up", **impostor
    )
    stranger = book_new_patient(store, second.slot_id)
    assert booking.patient_id != MARIA.patient_id
    assert booking.model_dump().keys() == stranger.model_dump().keys()
    assert booking.identity_verified == stranger.identity_verified

    lookup = store.lookup(
        date_of_birth=MARIA.date_of_birth, confirmation_code=booking.confirmation_code
    )
    assert [a.confirmation_code for a in lookup.appointments] == [booking.confirmation_code]
    with pytest.raises(IdentityVerificationError):
        store.cancel(confirmation_code="LKC-7Q2M", date_of_birth="1990-06-15")
    mine = store.lookup(date_of_birth=MARIA.date_of_birth, phone=MARIA.phone)
    assert [a.confirmation_code for a in mine.appointments] == ["LKC-7Q2M"]


def test_an_established_patients_name_and_dob_do_not_unlock_closed_providers(
    store: ClinicStore,
) -> None:
    slot = store.find_slots(provider_id="PR-06")[0]
    with pytest.raises(ClinicError, match="established patients"):
        store.book(
            slot_id=slot.slot_id,
            patient_name=MARIA.full_name,
            date_of_birth=MARIA.date_of_birth,
            phone="+15550108888",
            reason="annual exam",
        )


def test_a_different_name_with_the_same_dob_and_phone_is_a_different_patient(
    store: ClinicStore,
) -> None:
    slot = store.find_slots(provider_id="PR-05")[0]
    twin = store.book(
        slot_id=slot.slot_id,
        patient_name="Lucia Gonzalez",
        date_of_birth=MARIA.date_of_birth,
        phone=MARIA.phone,
        reason="follow-up",
    )
    assert twin.patient_id != MARIA.patient_id
    (owner,) = rows(
        store.path, f"SELECT full_name FROM patients WHERE patient_id = '{twin.patient_id}'"
    )
    assert owner == ("Lucia Gonzalez",)
    maria = store.lookup(date_of_birth=MARIA.date_of_birth, confirmation_code="LKC-7Q2M")
    assert [a.confirmation_code for a in maria.appointments] == ["LKC-7Q2M"]


def test_provider_not_accepting_new_patients(store: ClinicStore) -> None:
    slot = store.find_slots(provider_id="PR-06")[0]
    with pytest.raises(ClinicError, match="established patients"):
        book_new_patient(store, slot.slot_id)
    booking = store.book(
        slot_id=slot.slot_id,
        patient_name=MARIA.full_name,
        date_of_birth=MARIA.date_of_birth,
        phone=MARIA.phone,
        reason="annual exam",
    )
    assert booking.provider_name == "Alex Moreau, NP"
    assert rows(store.path, "SELECT COUNT(*) FROM patients")[0][0] == len(fixtures.PATIENTS)


def test_taken_held_past_and_unknown_slots_cannot_be_booked(store: ClinicStore) -> None:
    sql = "SELECT slot_id FROM slots WHERE held = 1 AND local_date > '2026-11-02' LIMIT 1"
    held = rows(store.path, sql)[0][0]
    for slot_id, error in [
        ("SL-PR02-20261103-1430", SlotUnavailableError),  # seed appointment
        (held, SlotUnavailableError),
        ("SL-PR01-20261102-0930", ClinicError),  # within the lead time
        ("SL-PR01-20261108-1000", ClinicError),  # Sunday: no such slot
    ]:
        with pytest.raises(error):
            book_new_patient(store, slot_id)


def test_a_patient_cannot_be_double_booked_at_the_same_time(store: ClinicStore) -> None:
    tuesday = {"date_from": "2026-11-03", "date_to": "2026-11-03", "limit": 10}
    raman = store.find_slots(provider_id="PR-01", **tuesday)
    okafor = {s.local_time: s for s in store.find_slots(provider_id="PR-02", **tuesday)}
    clash = next(s for s in raman if s.local_time in okafor)
    book_new_patient(store, clash.slot_id)
    with pytest.raises(ClinicError, match="already has an appointment"):
        book_new_patient(store, okafor[clash.local_time].slot_id)


def test_concurrent_bookings_of_one_slot_have_exactly_one_winner(store: ClinicStore) -> None:
    slot_id = store.find_slots(specialty="dermatology")[0].slot_id
    barrier = threading.Barrier(8)
    outcomes: list[str] = []

    def attempt(n: int) -> None:
        competitor = ClinicStore(store.path)  # separate connections, as separate requests use
        barrier.wait()
        try:
            competitor.book(
                slot_id=slot_id,
                patient_name=f"Racer {'abcdefgh'[n]}son",
                date_of_birth="1990-01-01",
                phone=f"+1555010700{n}",
                reason="race",
            )
            outcomes.append("booked")
        except SlotUnavailableError:
            outcomes.append("taken")

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Counter(outcomes) == {"booked": 1, "taken": 7}
    sql = f"SELECT COUNT(*) FROM appointments WHERE slot_id = '{slot_id}' AND status = 'booked'"
    assert rows(store.path, sql)[0][0] == 1


def test_database_constraint_rejects_double_booking_even_without_app_checks(
    store: ClinicStore,
) -> None:
    with sqlite3.connect(store.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO appointments (confirmation_code, patient_id, slot_id, reason, status,"
            " created_at, updated_at) VALUES ('LKC-ZZZZ', 'P-1003', 'SL-PR02-20261103-1430',"
            " 'x', 'booked', '', '')"
        )


# --------------------------------------------------------------------------- identity


def test_lookup_by_code_or_phone(store: ClinicStore) -> None:
    by_code = store.lookup(date_of_birth=JAMES.date_of_birth, confirmation_code="LKC-4H8T")
    by_phone = store.lookup(date_of_birth=JAMES.date_of_birth, phone=JAMES.phone)
    assert by_code == by_phone
    assert by_code.patient_name == "James Whitfield"
    assert [a.confirmation_code for a in by_code.appointments] == ["LKC-4H8T"]


@pytest.mark.parametrize(
    "attempt",
    [
        {"date_of_birth": "1972-11-03", "confirmation_code": "LKC-4H8T"},  # wrong DOB
        {"date_of_birth": "1972-11-02", "confirmation_code": "LKC-AAAA"},  # unknown code
        {"date_of_birth": "1985-03-14", "confirmation_code": "LKC-4H8T"},  # someone else's code
        {"date_of_birth": "1972-11-02", "phone": "+15550101003"},  # wrong phone
        {"date_of_birth": "2001-01-01", "phone": "+15550101002"},  # unknown person
    ],
)
def test_identity_failures_are_indistinguishable(store: ClinicStore, attempt: dict) -> None:
    with pytest.raises(IdentityVerificationError) as raised:
        store.lookup(**attempt)
    assert str(raised.value) == IDENTITY_FAILED_MESSAGE


def test_changes_require_the_matching_date_of_birth(store: ClinicStore) -> None:
    slot = store.find_slots(provider_id="PR-02")[0]
    with pytest.raises(IdentityVerificationError):
        store.reschedule(
            confirmation_code="LKC-4H8T", date_of_birth="1972-11-03", new_slot_id=slot.slot_id
        )
    with pytest.raises(IdentityVerificationError):
        store.cancel(confirmation_code="LKC-4H8T", date_of_birth="1999-07-21")


# --------------------------------------------------------------------------- reschedule


def test_reschedule_moves_the_booking_and_frees_the_old_slot(store: ClinicStore) -> None:
    new_slot = store.find_slots(provider_id="PR-02", date_from="2026-11-05")[0]
    moved = store.reschedule(
        confirmation_code="LKC-4H8T",
        date_of_birth=JAMES.date_of_birth,
        new_slot_id=new_slot.slot_id,
    )
    assert moved.old_start == "2026-11-03T14:30:00-05:00"
    assert moved.new_start == new_slot.start
    again = store.reschedule(
        confirmation_code="LKC-4H8T",
        date_of_birth=JAMES.date_of_birth,
        new_slot_id=new_slot.slot_id,
    )
    assert again.new_start == new_slot.start  # idempotent
    offered = {s.slot_id for s in store.find_slots(provider_id="PR-02", limit=10)}
    assert "SL-PR02-20261103-1430" in offered
    lookup = store.lookup(date_of_birth=JAMES.date_of_birth, confirmation_code="LKC-4H8T")
    assert [a.local_date for a in lookup.appointments] == [new_slot.local_date]


def test_cannot_reschedule_into_a_taken_slot_or_a_cancelled_booking(store: ClinicStore) -> None:
    with pytest.raises(SlotUnavailableError):
        store.reschedule(
            confirmation_code="LKC-4H8T",
            date_of_birth=JAMES.date_of_birth,
            new_slot_id="SL-PR01-20261105-1000",  # Maria's appointment
        )
    store.cancel(confirmation_code="LKC-4H8T", date_of_birth=JAMES.date_of_birth)
    slot = store.find_slots(provider_id="PR-02")[0]
    with pytest.raises(ClinicError, match="cancelled"):
        store.reschedule(
            confirmation_code="LKC-4H8T",
            date_of_birth=JAMES.date_of_birth,
            new_slot_id=slot.slot_id,
        )


# --------------------------------------------------------------------------- cancel


def test_cancelling_more_than_24_hours_ahead_is_free(store: ClinicStore) -> None:
    result = store.cancel(confirmation_code="LKC-4H8T", date_of_birth=JAMES.date_of_birth)
    assert result.status == "cancelled"
    assert (result.late_cancellation, result.fee_usd) == (False, 0.0)  # 29.5 h ahead
    reopened = store.find_slots(provider_id="PR-02", date_to="2026-11-03", limit=10)
    assert "SL-PR02-20261103-1430" in {s.slot_id for s in reopened}


def test_cancelling_within_24_hours_is_late_and_charged(store: ClinicStore) -> None:
    today = store.find_slots(date_from="2026-11-02", date_to="2026-11-02", time_of_day="afternoon")
    booking = book_new_patient(store, today[0].slot_id)
    result = store.cancel(confirmation_code=booking.confirmation_code, date_of_birth="1990-06-15")
    assert (result.late_cancellation, result.fee_usd) == (True, LATE_CANCELLATION_FEE_USD)
    repeat = store.cancel(confirmation_code=booking.confirmation_code, date_of_birth="1990-06-15")
    assert repeat == result  # idempotent: no second fee, same answer
