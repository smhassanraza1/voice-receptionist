import hashlib
import re
from contextlib import ExitStack

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

from receptionist.mcp_server.auth import APPOINTMENTS_WRITE, KB_READ
from receptionist.mcp_server.db import ClinicError, IdentityVerificationError
from receptionist.mcp_server.security import (
    FORBIDDEN_MESSAGE,
    INTERNAL_ERROR_MESSAGE,
    LOCKED_OUT_MESSAGE,
    RATE_LIMITED_MESSAGE,
    LockoutTracker,
    RateLimiter,
    ToolGuard,
    mask_arguments,
    mask_code,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def as_token():
    """Run code as if an HTTP request had authenticated with the given scopes."""
    tokens = []

    def authenticate(*scopes: str, client_id: str = "voice-agent") -> None:
        token = AccessToken(token="t", client_id=client_id, scopes=list(scopes))
        tokens.append(auth_context_var.set(AuthenticatedUser(token)))

    yield authenticate
    for token in reversed(tokens):
        auth_context_var.reset(token)


def make_guard(fake_clock: FakeClock, *, burst: int = 100) -> ToolGuard:
    return ToolGuard(
        limiter=RateLimiter(per_minute=60, burst=burst, clock=fake_clock),
        lockout=LockoutTracker(max_failures=2, window_seconds=900, clock=fake_clock),
    )


def test_rate_limiter_allows_a_burst_then_refills(fake_clock: FakeClock) -> None:
    limiter = RateLimiter(per_minute=60, burst=3, clock=fake_clock)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b")  # buckets are per client
    fake_clock.now += 1.0  # 60/min refills one call per second
    assert limiter.allow("a")
    assert not limiter.allow("a")


def fail_check(lockout: LockoutTracker, key: str) -> bool:
    assert lockout.begin(key)
    return lockout.finish(key, failed=True)


def test_lockout_triggers_after_max_failures_and_expires(fake_clock: FakeClock) -> None:
    lockout = LockoutTracker(max_failures=3, window_seconds=900, clock=fake_clock)
    assert [fail_check(lockout, "a") for _ in range(3)] == [False, False, True]
    assert lockout.is_locked("a") and not lockout.is_locked("b")
    assert not lockout.begin("a")
    fake_clock.now += 901
    assert not lockout.is_locked("a")


def test_lockout_failures_age_out_of_the_window(fake_clock: FakeClock) -> None:
    lockout = LockoutTracker(max_failures=2, window_seconds=60, clock=fake_clock)
    fail_check(lockout, "a")
    fake_clock.now += 61
    assert not fail_check(lockout, "a")
    assert not lockout.is_locked("a")


def test_checks_in_flight_count_against_the_limit(fake_clock: FakeClock) -> None:
    lockout = LockoutTracker(max_failures=2, window_seconds=900, clock=fake_clock)
    assert lockout.begin("a") and lockout.begin("a")
    assert not lockout.begin("a")  # a third parallel guess would exceed max_failures
    assert not lockout.finish("a", failed=False)
    assert lockout.begin("a")  # a check that succeeded frees its place
    assert [lockout.finish("a", failed=True) for _ in range(2)] == [False, True]
    assert lockout.is_locked("a")


def test_expired_lockout_records_are_dropped(fake_clock: FakeClock) -> None:
    lockout = LockoutTracker(max_failures=2, window_seconds=60, clock=fake_clock)
    for key in ("a", "b", "c"):
        fail_check(lockout, key)
    fail_check(lockout, "c")  # locked out
    fake_clock.now += 61
    assert lockout.begin("d")
    assert set(lockout._records) == {"d"}


def test_masking_keeps_identifiers_out_of_the_audit_log() -> None:
    masked = mask_arguments(
        {
            "patient_name": "Margaret O'Neill",
            "date_of_birth": "1948-01-30",
            "phone": "+15550101005",
            "confirmation_code": "LKC-2B6D",
            "query": "my daughter's rash",
            "slot_id": "SL-PR04-20261105-1100",
            "provider_id": None,
        }
    )
    fingerprint = masked.pop("confirmation_code")
    assert masked == {
        "patient_name": "M. O.",
        "date_of_birth": "1948-**-**",
        "phone": "**********05",
        "query": "<18 chars>",
        "slot_id": "SL-PR04-20261105-1100",
    }
    # A code has under a million values, so a plain hash could be reversed by trying them all;
    # the fingerprint is keyed per process and only correlates calls within one run.
    assert re.fullmatch(r"#[0-9a-f]{12}", fingerprint)
    assert mask_code("LKC-2B6D") == fingerprint != mask_code("LKC-2B6E")
    assert fingerprint != "#" + hashlib.sha256(b"LKC-2B6D").hexdigest()[:12]


def test_guard_enforces_tool_scopes(fake_clock: FakeClock, as_token) -> None:
    guard = make_guard(fake_clock)
    as_token(KB_READ)
    with guard.call("search_knowledge_base", KB_READ, None) as caller:
        assert caller.principal == "voice-agent"
    with (
        pytest.raises(ToolError, match=FORBIDDEN_MESSAGE),
        guard.call("book_appointment", APPOINTMENTS_WRITE, None),
    ):
        pytest.fail("the tool body must not run")


def test_guard_rate_limits_authenticated_clients(fake_clock: FakeClock, as_token) -> None:
    guard = make_guard(fake_clock, burst=2)
    as_token(KB_READ)
    for _ in range(2):
        with guard.call("search_knowledge_base", KB_READ, None):
            pass
    with (
        pytest.raises(ToolError, match=RATE_LIMITED_MESSAGE),
        guard.call("search_knowledge_base", KB_READ, None),
    ):
        pass


def test_guard_maps_errors_and_counts_identity_failures(fake_clock: FakeClock) -> None:
    guard = make_guard(fake_clock)  # in-process caller: every scope, no rate limit
    with pytest.raises(ToolError, match="Slot gone"), guard.call("x", KB_READ, None):
        raise ClinicError("Slot gone.")
    for _ in range(2):
        with pytest.raises(ToolError), guard.call("x", KB_READ, None, verifies_identity=True):
            raise IdentityVerificationError()
    with (
        pytest.raises(ToolError, match=LOCKED_OUT_MESSAGE),
        guard.call("x", KB_READ, None, verifies_identity=True),
    ):
        pytest.fail("locked out callers never reach the tool body")
    with pytest.raises(ToolError) as crash, guard.call("x", KB_READ, None):
        raise KeyError("internal detail")
    assert str(crash.value) == INTERNAL_ERROR_MESSAGE


def test_guard_holds_parallel_identity_checks_to_the_limit(fake_clock: FakeClock) -> None:
    guard = make_guard(fake_clock)  # max_failures=2
    with ExitStack() as running:
        for _ in range(2):
            running.enter_context(guard.call("x", KB_READ, None, verifies_identity=True))
        with (
            pytest.raises(ToolError, match=LOCKED_OUT_MESSAGE),
            guard.call("x", KB_READ, None, verifies_identity=True),
        ):
            pytest.fail("a check beyond the limit must not reach the tool body")
    with guard.call("x", KB_READ, None, verifies_identity=True):
        pass  # both earlier checks succeeded, so nothing counts against the caller
