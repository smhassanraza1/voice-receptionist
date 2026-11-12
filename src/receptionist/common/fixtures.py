"""Shared, fictional seed data.

The MCP server seeds its database from here, the billing agent seeds its ledger from here, and
the eval scenarios reference these exact people. All names, numbers and records are invented.
Phone numbers use the reserved 555-01xx range.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CLINIC = {
    "name": "Lakeside Family Clinic",
    "address": "120 Harbor Street, Suite 200, Lakeside",
    "phone": "+15550100000",
    "timezone": "America/New_York",
    # weekday() -> (open, close) local time; Sunday closed
    "hours": {
        0: ("08:00", "18:00"),
        1: ("08:00", "18:00"),
        2: ("08:00", "18:00"),
        3: ("08:00", "18:00"),
        4: ("08:00", "18:00"),
        5: ("09:00", "13:00"),
    },
    "slot_minutes": 30,
    "booking_horizon_days": 28,
}


@dataclass(frozen=True)
class Provider:
    provider_id: str
    name: str
    specialty: str
    languages: tuple[str, ...]
    # weekdays (0=Mon) this provider sees patients
    workdays: tuple[int, ...]
    accepts_new_patients: bool = True


PROVIDERS: tuple[Provider, ...] = (
    Provider("PR-01", "Dr. Priya Raman", "family_medicine", ("en", "hi"), (0, 1, 2, 3, 4)),
    Provider("PR-02", "Dr. Daniel Okafor", "cardiology", ("en",), (1, 3)),
    Provider("PR-03", "Dr. Sofia Marquez", "pediatrics", ("en", "es"), (0, 2, 4, 5)),
    Provider("PR-04", "Dr. Hannah Weiss", "dermatology", ("en", "de"), (0, 3)),
    Provider("PR-05", "Dr. Kenji Watanabe", "family_medicine", ("en", "ja"), (1, 2, 4, 5)),
    Provider("PR-06", "Alex Moreau, NP", "womens_health", ("en", "fr"), (0, 1, 4), False),
)

SPECIALTIES = sorted({p.specialty for p in PROVIDERS})


@dataclass(frozen=True)
class Patient:
    patient_id: str
    full_name: str
    date_of_birth: str  # YYYY-MM-DD
    phone: str  # E.164
    preferred_language: str = "en"


PATIENTS: tuple[Patient, ...] = (
    Patient("P-1001", "Maria Gonzalez", "1985-03-14", "+15550101001", "es"),
    Patient("P-1002", "James Whitfield", "1972-11-02", "+15550101002"),
    Patient("P-1003", "Aisha Rahman", "1999-07-21", "+15550101003"),
    Patient("P-1004", "Liam Chen", "2018-05-09", "+15550101004"),
    Patient("P-1005", "Margaret O'Neill", "1948-01-30", "+15550101005"),
)


@dataclass(frozen=True)
class SeedAppointment:
    confirmation_code: str
    patient_id: str
    provider_id: str
    # offset from the clinic clock's *date*, plus a local start time
    day_offset: int
    start_time: str  # HH:MM local
    reason: str


# Offsets are relative to clock.now().date(); with CLINIC_NOW=EVAL_NOW (Mon 2026-11-02)
# they land on valid workdays for each provider.
SEED_APPOINTMENTS: tuple[SeedAppointment, ...] = (
    SeedAppointment("LKC-7Q2M", "P-1001", "PR-01", 3, "10:00", "annual physical"),
    SeedAppointment("LKC-4H8T", "P-1002", "PR-02", 1, "14:30", "blood pressure follow-up"),
    SeedAppointment("LKC-9P3K", "P-1004", "PR-03", 2, "09:30", "school vaccination check"),
    SeedAppointment("LKC-2B6D", "P-1005", "PR-04", 3, "11:00", "skin check"),
)


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    service_date_offset: int  # days relative to clinic date (negative = past)
    description: str
    amount_billed: float
    insurance_paid: float
    patient_paid: float

    @property
    def balance(self) -> float:
        return round(self.amount_billed - self.insurance_paid - self.patient_paid, 2)


@dataclass(frozen=True)
class BillingAccount:
    patient_id: str
    insurance_plan: str | None
    invoices: tuple[Invoice, ...] = field(default_factory=tuple)
    # claim_id -> status ("submitted" | "in_review" | "approved" | "denied")
    claims: dict[str, str] = field(default_factory=dict)
    on_payment_plan: bool = False


BILLING_ACCOUNTS: tuple[BillingAccount, ...] = (
    BillingAccount(
        "P-1001",
        "BlueHarbor PPO",
        (Invoice("INV-5001", -40, "Office visit", 180.00, 140.00, 40.00),),
    ),
    BillingAccount(
        "P-1002",
        "Medicare Part B",
        (
            Invoice("INV-5002", -62, "Echocardiogram", 950.00, 764.60, 0.00),
            Invoice("INV-5003", -21, "Follow-up visit", 210.00, 168.00, 42.00),
        ),
    ),
    BillingAccount(
        "P-1003",
        "Northwind HMO",
        (Invoice("INV-5004", -9, "New patient visit", 260.00, 0.00, 0.00),),
        claims={"CLM-8801": "in_review"},
    ),
    BillingAccount("P-1004", "BlueHarbor PPO", ()),
    BillingAccount(
        "P-1005",
        "Medicare Part B",
        (Invoice("INV-5005", -75, "Biopsy", 420.00, 336.00, 25.00),),
        on_payment_plan=True,
    ),
)

ACCEPTED_INSURANCE = (
    "BlueHarbor PPO",
    "Northwind HMO",
    "Medicare Part B",
    "Evergreen Health Choice",
    "Summit Mutual EPO",
)


def patient_by_id(patient_id: str) -> Patient | None:
    return next((p for p in PATIENTS if p.patient_id == patient_id), None)
