"""SQLite storage and the clinic's booking rules.

The schedule is a table of 30-minute slots per provider. A slot is free when it is not `held`
(taken by bookings outside this system, simulated deterministically at seed time) and no
`booked` appointment points at it. A partial unique index makes double-booking impossible even
across processes; writes also run under `BEGIN IMMEDIATE` so the friendly checks see a
consistent view.

Times are stored as UTC ISO-8601 strings (lexically sortable) alongside the local clinic
date/time used for filtering. "Now" always comes from `receptionist.common.clock`.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from receptionist.common import clock, fixtures

from .models import (
    AppointmentLookup,
    AppointmentSummary,
    BookingConfirmation,
    CancellationConfirmation,
    ProviderInfo,
    RescheduleConfirmation,
    Slot,
)
from .validation import name_key

logger = logging.getLogger(__name__)

BOOKING_HORIZON = timedelta(days=fixtures.CLINIC["booking_horizon_days"])
SLOT_LENGTH = timedelta(minutes=fixtures.CLINIC["slot_minutes"])
BOOKING_LEAD_TIME = timedelta(hours=1)
LUNCH_BREAK = (time(12, 0), time(13, 0))  # weekdays only
HELD_SLOT_PERCENT = 35
LATE_CANCELLATION_WINDOW = timedelta(hours=24)
LATE_CANCELLATION_FEE_USD = 50.0
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

IDENTITY_FAILED_MESSAGE = (
    "I couldn't verify that with the details given. Please double-check the date of birth "
    "and the confirmation code or phone number on file."
)

_SCHEMA = """
DROP TABLE IF EXISTS appointments;
DROP TABLE IF EXISTS slots;
DROP TABLE IF EXISTS patients;
DROP TABLE IF EXISTS providers;

CREATE TABLE providers (
    provider_id          TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    specialty            TEXT NOT NULL,
    languages            TEXT NOT NULL,          -- comma-separated ISO 639-1 codes
    accepts_new_patients INTEGER NOT NULL
);

CREATE TABLE patients (
    patient_id         TEXT PRIMARY KEY,
    full_name          TEXT NOT NULL,
    name_key           TEXT NOT NULL,            -- validation.name_key(full_name)
    date_of_birth      TEXT NOT NULL,
    phone              TEXT NOT NULL,            -- E.164
    preferred_language TEXT NOT NULL DEFAULT 'en',
    is_established     INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);
CREATE INDEX patients_by_dob ON patients (date_of_birth);

CREATE TABLE slots (
    slot_id     TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers (provider_id),
    starts_at   TEXT NOT NULL,                   -- UTC
    ends_at     TEXT NOT NULL,                   -- UTC
    local_date  TEXT NOT NULL,                   -- YYYY-MM-DD, clinic time
    local_time  TEXT NOT NULL,                   -- HH:MM, clinic time
    held        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX slots_by_date ON slots (local_date, provider_id);

CREATE TABLE appointments (
    appointment_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT NOT NULL UNIQUE,
    patient_id        TEXT NOT NULL REFERENCES patients (patient_id),
    slot_id           TEXT NOT NULL REFERENCES slots (slot_id),
    reason            TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('booked', 'cancelled')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    cancel_reason     TEXT,
    late_cancellation INTEGER NOT NULL DEFAULT 0,
    fee_usd           REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX one_booking_per_slot ON appointments (slot_id) WHERE status = 'booked';
CREATE INDEX appointments_by_patient ON appointments (patient_id);
"""

_SLOT_COLUMNS = """
    s.slot_id, s.provider_id, s.starts_at, s.ends_at, s.local_date, s.local_time, s.held,
    p.name AS provider_name, p.specialty, p.accepts_new_patients
"""


class ClinicError(Exception):
    """A business-rule failure. The message is short and safe to read out to a caller."""


class SlotUnavailableError(ClinicError):
    def __init__(self) -> None:
        super().__init__(
            "Sorry, that time is no longer available. Please search again and offer another time."
        )


class IdentityVerificationError(ClinicError):
    """Date of birth plus code/phone did not match. Deliberately says nothing more."""

    def __init__(self) -> None:
        super().__init__(IDENTITY_FAILED_MESSAGE)


# --------------------------------------------------------------------------- seeding


def reset_database(path: str | Path) -> None:
    """(Re)create the database at `path` and seed it from the fixtures and the clinic clock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = clock.now()
    with closing(_connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        # One transaction for DDL and data: a failed reseed leaves the old database intact.
        conn.executescript("BEGIN IMMEDIATE;" + _SCHEMA)
        conn.executemany(
            "INSERT INTO providers VALUES (?, ?, ?, ?, ?)",
            [
                (p.provider_id, p.name, p.specialty, ",".join(p.languages), p.accepts_new_patients)
                for p in fixtures.PROVIDERS
            ],
        )
        conn.executemany(
            "INSERT INTO patients VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            [
                (
                    p.patient_id,
                    p.full_name,
                    name_key(p.full_name),
                    p.date_of_birth,
                    p.phone,
                    p.preferred_language,
                    _iso_utc(now),
                )
                for p in fixtures.PATIENTS
            ],
        )
        _insert_slots(conn, now.date())
        _seed_appointments(conn, now)
        conn.execute("COMMIT")
    logger.info("Seeded clinic database at %s for %s", path, now.date())


def _insert_slots(conn: sqlite3.Connection, today: date) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO slots VALUES (?, ?, ?, ?, ?, ?, ?)", _generate_slots(today)
    )


def _generate_slots(today: date) -> Iterator[tuple[str, str, str, str, str, str, bool]]:
    """Every provider's slots from `today` through the booking horizon, during clinic hours."""
    tz = clock.clinic_tz()
    for offset in range(BOOKING_HORIZON.days + 1):
        day = today + timedelta(days=offset)
        hours = fixtures.CLINIC["hours"].get(day.weekday())
        if hours is None:
            continue
        opens, closes = (datetime.combine(day, time.fromisoformat(h), tz) for h in hours)
        for provider in fixtures.PROVIDERS:
            if day.weekday() not in provider.workdays:
                continue
            start = opens
            while start + SLOT_LENGTH <= closes:
                on_lunch = day.weekday() < 5 and LUNCH_BREAK[0] <= start.time() < LUNCH_BREAK[1]
                if not on_lunch:
                    slot_id = f"SL-{provider.provider_id.replace('-', '')}-{start:%Y%m%d-%H%M}"
                    yield (
                        slot_id,
                        provider.provider_id,
                        _iso_utc(start),
                        _iso_utc(start + SLOT_LENGTH),
                        day.isoformat(),
                        f"{start:%H:%M}",
                        _is_held(slot_id),
                    )
                start += SLOT_LENGTH


def _is_held(slot_id: str) -> bool:
    """Stable pseudo-random ~35% of slots are taken, so availability looks realistic."""
    digest = hashlib.sha256(slot_id.encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100 < HELD_SLOT_PERCENT


def _seed_appointments(conn: sqlite3.Connection, now: datetime) -> None:
    for seed in fixtures.SEED_APPOINTMENTS:
        target = (now.date() + timedelta(days=seed.day_offset)).isoformat()
        # With CLINIC_NOW=EVAL_NOW the target day is a workday; otherwise use the next one.
        row = conn.execute(
            "SELECT slot_id FROM slots WHERE provider_id = ? AND local_time = ? AND local_date >= ?"
            " ORDER BY local_date LIMIT 1",
            (seed.provider_id, seed.start_time, target),
        ).fetchone()
        if row is None:
            logger.warning("No slot for seed appointment %s", seed.confirmation_code)
            continue
        conn.execute("UPDATE slots SET held = 0 WHERE slot_id = ?", (row["slot_id"],))
        conn.execute(
            "INSERT INTO appointments (confirmation_code, patient_id, slot_id, reason, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'booked', ?, ?)",
            (
                seed.confirmation_code,
                seed.patient_id,
                row["slot_id"],
                seed.reason,
                _iso_utc(now),
                _iso_utc(now),
            ),
        )


# --------------------------------------------------------------------------- store


class ClinicStore:
    """Booking operations over the SQLite schedule. Safe to share across threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self._has_schema():
            reset_database(self.path)
        self._schedule_from: date | None = None
        self._extend_schedule()

    # -- reads -----------------------------------------------------------------------------

    def list_providers(
        self, specialty: str | None = None, language: str | None = None
    ) -> list[ProviderInfo]:
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM providers ORDER BY provider_id").fetchall()
        providers = [
            ProviderInfo(
                provider_id=r["provider_id"],
                name=r["name"],
                specialty=r["specialty"],
                languages=r["languages"].split(","),
                accepts_new_patients=bool(r["accepts_new_patients"]),
            )
            for r in rows
        ]
        return [
            p
            for p in providers
            if (specialty is None or p.specialty == specialty)
            and (language is None or language in p.languages)
        ]

    def find_slots(
        self,
        *,
        specialty: str | None = None,
        provider_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        time_of_day: str = "any",
        limit: int = 5,
    ) -> list[Slot]:
        now = clock.now()
        self._extend_schedule()
        first, last = _search_window(now.date(), date_from, date_to)
        with self._transaction() as conn:
            if provider_id is not None:
                provider = conn.execute(
                    "SELECT name, specialty FROM providers WHERE provider_id = ?", (provider_id,)
                ).fetchone()
                if provider is None:
                    raise ClinicError(
                        "There is no provider with that id. Use list_providers to see who is available."
                    )
                if specialty is not None and provider["specialty"] != specialty:
                    raise ClinicError(
                        f"{provider['name']} practises {_label(provider['specialty'])}, "
                        f"not {_label(specialty)}."
                    )
            rows = conn.execute(
                f"""
                SELECT {_SLOT_COLUMNS}
                FROM slots s JOIN providers p ON p.provider_id = s.provider_id
                WHERE s.held = 0
                  AND s.starts_at >= :earliest
                  AND s.local_date BETWEEN :first AND :last
                  AND (:provider_id IS NULL OR s.provider_id = :provider_id)
                  AND (:specialty IS NULL OR p.specialty = :specialty)
                  AND (:time_of_day = 'any'
                       OR (:time_of_day = 'morning' AND s.local_time < '12:00')
                       OR (:time_of_day = 'afternoon' AND s.local_time >= '12:00'))
                  AND NOT EXISTS (SELECT 1 FROM appointments a
                                  WHERE a.slot_id = s.slot_id AND a.status = 'booked')
                ORDER BY s.starts_at, s.provider_id
                LIMIT :limit
                """,
                {
                    "earliest": _iso_utc(now + BOOKING_LEAD_TIME),
                    "first": first.isoformat(),
                    "last": last.isoformat(),
                    "provider_id": provider_id,
                    "specialty": specialty,
                    "time_of_day": time_of_day,
                    "limit": limit,
                },
            ).fetchall()
        return [_slot_model(r) for r in rows]

    def lookup(
        self, *, date_of_birth: str, confirmation_code: str | None = None, phone: str | None = None
    ) -> AppointmentLookup:
        if not confirmation_code and not phone:
            raise ClinicError(
                "To look up appointments I need the date of birth plus the confirmation code "
                "or the phone number on file."
            )
        now = clock.now()
        with self._transaction() as conn:
            patient = _verify_patient(conn, date_of_birth, confirmation_code, phone)
            rows = conn.execute(
                f"""
                SELECT a.confirmation_code, a.status, a.reason, {_SLOT_COLUMNS}
                FROM appointments a
                JOIN slots s ON s.slot_id = a.slot_id
                JOIN providers p ON p.provider_id = s.provider_id
                WHERE a.patient_id = ? AND s.starts_at >= ?
                ORDER BY s.starts_at
                """,
                (patient["patient_id"], _iso_utc(now)),
            ).fetchall()
        return AppointmentLookup(
            patient_id=patient["patient_id"],
            patient_name=patient["full_name"],
            appointments=[
                AppointmentSummary(
                    confirmation_code=r["confirmation_code"],
                    provider_name=r["provider_name"],
                    specialty=r["specialty"],
                    start=_local_iso(r["starts_at"]),
                    local_date=r["local_date"],
                    local_time=r["local_time"],
                    status=r["status"],
                    reason=r["reason"],
                )
                for r in rows
            ],
        )

    # -- writes ----------------------------------------------------------------------------

    def book(
        self, *, slot_id: str, patient_name: str, date_of_birth: str, phone: str, reason: str
    ) -> BookingConfirmation:
        now = clock.now()
        with self._transaction(write=True) as conn:
            slot = _bookable_slot(conn, slot_id, now)
            patient = _find_patient(conn, patient_name, date_of_birth, phone)
            _check_accepts_patient(slot, patient)
            if patient is None:
                patient_id = _create_patient(conn, patient_name, date_of_birth, phone, now)
            else:
                patient_id = patient["patient_id"]
                _check_no_overlap(conn, patient_id, slot)
            code = _new_confirmation_code(conn)
            try:
                cursor = conn.execute(
                    "INSERT INTO appointments (confirmation_code, patient_id, slot_id, reason,"
                    " status, created_at, updated_at) VALUES (?, ?, ?, ?, 'booked', ?, ?)",
                    (code, patient_id, slot_id, reason, _iso_utc(now), _iso_utc(now)),
                )
            except sqlite3.IntegrityError:
                raise SlotUnavailableError() from None
        return BookingConfirmation(
            appointment_id=f"APT-{cursor.lastrowid:06d}",
            confirmation_code=code,
            provider_name=slot["provider_name"],
            start=_local_iso(slot["starts_at"]),
            local_date=slot["local_date"],
            local_time=slot["local_time"],
            patient_id=patient_id,
        )

    def reschedule(
        self, *, confirmation_code: str, date_of_birth: str, new_slot_id: str
    ) -> RescheduleConfirmation:
        now = clock.now()
        with self._transaction(write=True) as conn:
            appt = _verified_appointment(conn, confirmation_code, date_of_birth)
            if appt["status"] == "cancelled":
                raise ClinicError(
                    "That appointment was cancelled, so it can't be moved. I can book a new one instead."
                )
            if _parse_utc(appt["starts_at"]) <= now:
                raise ClinicError("That appointment time has already passed.")
            slot = appt  # moving to the slot it already has is an idempotent no-op
            if new_slot_id != appt["slot_id"]:
                slot = _bookable_slot(conn, new_slot_id, now)
                patient = conn.execute(
                    "SELECT * FROM patients WHERE patient_id = ?", (appt["patient_id"],)
                ).fetchone()
                _check_accepts_patient(slot, patient)
                _check_no_overlap(conn, appt["patient_id"], slot, ignore=appt["appointment_id"])
                try:
                    conn.execute(
                        "UPDATE appointments SET slot_id = ?, updated_at = ? WHERE appointment_id = ?",
                        (new_slot_id, _iso_utc(now), appt["appointment_id"]),
                    )
                except sqlite3.IntegrityError:
                    raise SlotUnavailableError() from None
        return RescheduleConfirmation(
            confirmation_code=confirmation_code,
            old_start=_local_iso(appt["starts_at"]),
            new_start=_local_iso(slot["starts_at"]),
            provider_name=slot["provider_name"],
            local_date=slot["local_date"],
            local_time=slot["local_time"],
        )

    def cancel(
        self, *, confirmation_code: str, date_of_birth: str, reason: str | None = None
    ) -> CancellationConfirmation:
        now = clock.now()
        with self._transaction(write=True) as conn:
            appt = _verified_appointment(conn, confirmation_code, date_of_birth)
            start = _parse_utc(appt["starts_at"])
            if appt["status"] == "booked":  # cancelling twice returns the original outcome
                if start <= now:
                    raise ClinicError("That appointment time has already passed.")
                late = start - now < LATE_CANCELLATION_WINDOW
                fee = LATE_CANCELLATION_FEE_USD if late else 0.0
                conn.execute(
                    "UPDATE appointments SET status = 'cancelled', updated_at = ?,"
                    " cancel_reason = ?, late_cancellation = ?, fee_usd = ?"
                    " WHERE appointment_id = ?",
                    (_iso_utc(now), reason or None, late, fee, appt["appointment_id"]),
                )
            else:
                late, fee = bool(appt["late_cancellation"]), appt["fee_usd"]
        return CancellationConfirmation(
            confirmation_code=confirmation_code,
            start=_local_iso(appt["starts_at"]),
            late_cancellation=late,
            fee_usd=fee,
        )

    # -- plumbing --------------------------------------------------------------------------

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = _connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _has_schema(self) -> bool:
        if not self.path.exists():
            return False
        with closing(_connect(self.path)) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'appointments'"
            ).fetchone()
        return row is not None

    def _extend_schedule(self) -> None:
        """Add slots for days that entered the booking horizon since the database was seeded."""
        today = clock.now().date()
        if self._schedule_from == today:
            return
        with self._transaction(write=True) as conn:
            _insert_slots(conn, today)
        self._schedule_from = today


# --------------------------------------------------------------------------- helpers


def _connect(path: Path) -> sqlite3.Connection:
    # isolation_level=None: transactions are explicit (see ClinicStore._transaction).
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _search_window(today: date, date_from: str | None, date_to: str | None) -> tuple[date, date]:
    horizon_end = today + BOOKING_HORIZON
    first = date.fromisoformat(date_from) if date_from else today
    if first < today:
        raise ClinicError(f"That date is in the past. Today is {clock.today_str()}.")
    if first > horizon_end:
        raise ClinicError(
            f"Appointments can be booked up to {BOOKING_HORIZON.days} days ahead, "
            f"through {_spoken_date(horizon_end)}."
        )
    last = date.fromisoformat(date_to) if date_to else first + timedelta(days=13)
    if last < first:
        raise ClinicError("The end date must be on or after the start date.")
    return first, min(last, horizon_end)


def _bookable_slot(conn: sqlite3.Connection, slot_id: str, now: datetime) -> sqlite3.Row:
    slot = conn.execute(
        f"SELECT {_SLOT_COLUMNS} FROM slots s JOIN providers p ON p.provider_id = s.provider_id"
        " WHERE s.slot_id = ?",
        (slot_id,),
    ).fetchone()
    if slot is None:
        raise ClinicError("That slot id doesn't exist. Please search for available slots again.")
    if _parse_utc(slot["starts_at"]) < now + BOOKING_LEAD_TIME:
        raise ClinicError("That time is too soon or already past. Please choose a later slot.")
    taken = conn.execute(
        "SELECT 1 FROM appointments WHERE slot_id = ? AND status = 'booked'", (slot_id,)
    ).fetchone()
    if slot["held"] or taken:
        raise SlotUnavailableError()
    return slot


def _find_patient(
    conn: sqlite3.Connection, full_name: str, date_of_birth: str, phone: str
) -> sqlite3.Row | None:
    """The existing patient with this name, DOB and phone number, if there is one.

    All three must match. Name and DOB alone are not proof of identity, and a shared DOB and
    phone (twins, a parent booking for children) does not make two names one person. Anything
    short of a full match books under a new record, so the reply never says whether a partial
    match exists. A patient whose phone number changed therefore gets a second record, logged
    as a possible duplicate for staff to merge.
    """
    rows = conn.execute(
        "SELECT * FROM patients WHERE name_key = ? AND date_of_birth = ? ORDER BY patient_id",
        (name_key(full_name), date_of_birth),
    ).fetchall()
    match = next((r for r in rows if r["phone"] == phone), None)
    if match is None and rows:
        logger.info("New patient record may duplicate %s", ", ".join(r["patient_id"] for r in rows))
    return match


def _create_patient(
    conn: sqlite3.Connection, full_name: str, date_of_birth: str, phone: str, now: datetime
) -> str:
    (highest,) = conn.execute(
        "SELECT MAX(CAST(SUBSTR(patient_id, 3) AS INTEGER)) FROM patients"
    ).fetchone()
    patient_id = f"P-{(highest or 1000) + 1}"
    conn.execute(
        "INSERT INTO patients (patient_id, full_name, name_key, date_of_birth, phone,"
        " is_established, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
        (patient_id, full_name, name_key(full_name), date_of_birth, phone, _iso_utc(now)),
    )
    return patient_id


def _check_accepts_patient(slot: sqlite3.Row, patient: sqlite3.Row | None) -> None:
    if slot["accepts_new_patients"] or (patient is not None and patient["is_established"]):
        return
    raise ClinicError(
        f"{slot['provider_name']} is only seeing established patients right now. "
        "Please offer another provider."
    )


def _check_no_overlap(
    conn: sqlite3.Connection, patient_id: str, slot: sqlite3.Row, ignore: int | None = None
) -> None:
    clash = conn.execute(
        "SELECT 1 FROM appointments a JOIN slots s ON s.slot_id = a.slot_id"
        " WHERE a.patient_id = ? AND a.status = 'booked' AND s.starts_at = ?"
        " AND a.appointment_id IS NOT ?",
        (patient_id, slot["starts_at"], ignore),
    ).fetchone()
    if clash:
        raise ClinicError("This patient already has an appointment at that time.")


def _verify_patient(
    conn: sqlite3.Connection, date_of_birth: str, code: str | None, phone: str | None
) -> sqlite3.Row:
    if code:
        row = conn.execute(
            "SELECT p.* FROM appointments a JOIN patients p ON p.patient_id = a.patient_id"
            " WHERE a.confirmation_code = ? AND p.date_of_birth = ?",
            (code, date_of_birth),
        ).fetchone()
        if row is not None:
            return row
    if phone:
        rows = conn.execute(
            "SELECT * FROM patients WHERE date_of_birth = ? AND phone = ?", (date_of_birth, phone)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise ClinicError(
                "More than one patient shares that phone number and date of birth. "
                "Please use the confirmation code instead."
            )
    raise IdentityVerificationError()


def _verified_appointment(conn: sqlite3.Connection, code: str, date_of_birth: str) -> sqlite3.Row:
    row = conn.execute(
        f"""
        SELECT a.*, {_SLOT_COLUMNS}
        FROM appointments a
        JOIN patients pt ON pt.patient_id = a.patient_id
        JOIN slots s ON s.slot_id = a.slot_id
        JOIN providers p ON p.provider_id = s.provider_id
        WHERE a.confirmation_code = ? AND pt.date_of_birth = ?
        """,
        (code, date_of_birth),
    ).fetchone()
    if row is None:
        raise IdentityVerificationError()
    return row


def _new_confirmation_code(conn: sqlite3.Connection) -> str:
    while True:
        code = "LKC-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
        exists = conn.execute(
            "SELECT 1 FROM appointments WHERE confirmation_code = ?", (code,)
        ).fetchone()
        if not exists:
            return code


def _slot_model(row: sqlite3.Row) -> Slot:
    start = _parse_utc(row["starts_at"]).astimezone(clock.clinic_tz())
    return Slot(
        slot_id=row["slot_id"],
        provider_id=row["provider_id"],
        provider_name=row["provider_name"],
        specialty=row["specialty"],
        start=start.isoformat(),
        end=_local_iso(row["ends_at"]),
        weekday=f"{start:%A}",
        local_date=row["local_date"],
        local_time=row["local_time"],
    )


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _local_iso(utc_value: str) -> str:
    return _parse_utc(utc_value).astimezone(clock.clinic_tz()).isoformat()


def _spoken_date(day: date) -> str:
    return f"{day:%A}, {day:%B} {day.day}"


def _label(specialty: str) -> str:
    return specialty.replace("_", " ")
