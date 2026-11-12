"""Per-call protections shared by every tool: scopes, rate limits, lockout and the audit trail.

`ToolGuard.call()` wraps a tool body. It resolves who is calling, enforces the tool's scope,
the caller's rate limit and the identity-check lockout, turns domain errors into speakable tool
errors (never stack traces), and writes one PII-masked JSON audit line per call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from .auth import ALL_SCOPES
from .db import ClinicError, IdentityVerificationError

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("receptionist.audit")

LOCAL_PRINCIPAL = "local"
_FINGERPRINT_KEY = secrets.token_bytes(32)

FORBIDDEN_MESSAGE = (
    "This assistant isn't permitted to do that. Offer to transfer the caller to the front desk."
)
RATE_LIMITED_MESSAGE = "The clinic system is busy. Wait a few seconds, then try again."
LOCKED_OUT_MESSAGE = (
    "Too many unsuccessful verification attempts. For the patient's security, offer to "
    "transfer the caller to the front desk."
)
INTERNAL_ERROR_MESSAGE = (
    "Something went wrong on our side. Try once more, or offer to transfer the caller to the "
    "front desk."
)


@dataclass(frozen=True)
class Caller:
    """Who is calling: the token subject plus the MCP session, when the transport has one."""

    principal: str
    scopes: frozenset[str]
    session_id: str | None = None

    @property
    def key(self) -> str:
        """Lockout key. The voice agent opens one MCP session per phone call."""
        return f"{self.principal}:{self.session_id}" if self.session_id else self.principal


def current_caller(ctx: Context | None) -> Caller:
    headers: Mapping[str, str] = (ctx.headers if ctx is not None else None) or {}
    session_id = headers.get("mcp-session-id")
    token = get_access_token()
    if token is None:
        # No HTTP auth layer: an in-process Client(server) or stdio. Over HTTP the transport
        # rejects unauthenticated requests before a tool runs, so this caller already owns the
        # process and is trusted with every scope.
        return Caller(LOCAL_PRINCIPAL, frozenset(ALL_SCOPES), session_id)
    return Caller(token.client_id, frozenset(token.scopes), session_id)


class RateLimiter:
    """Token bucket per key: up to `burst` calls at once, refilled at `per_minute`."""

    def __init__(
        self, per_minute: float, burst: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = per_minute / 60
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self._clock()
            tokens, updated = self._buckets.get(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - updated) * self._rate)
            allowed = tokens >= 1
            self._buckets[key] = (tokens - 1 if allowed else tokens, now)
            return allowed


@dataclass
class _FailureRecord:
    failures: deque[float] = field(default_factory=deque)
    in_flight: int = 0
    locked_until: float = 0.0


class LockoutTracker:
    """Locks a key out of identity checks after `max_failures` within `window_seconds`.

    The guard keys it by `Caller.key`, i.e. per MCP session (one phone call), so a new session
    starts with a fresh count; the per-client rate limit is what bounds how many sessions, and
    so how many guesses, one client gets. Successes do not reset the count, so interleaving a
    known-good identity with guesses doesn't buy more guesses; failures simply age out of the
    window.

    A check holds a place from `begin` until `finish`, and every check still running counts as
    a failure, so parallel tool calls cannot get more than `max_failures` guesses in.
    """

    def __init__(
        self,
        max_failures: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_failures = max_failures
        self._window = window_seconds
        self._clock = clock
        self._records: dict[str, _FailureRecord] = {}
        self._lock = threading.Lock()

    def is_locked(self, key: str) -> bool:
        with self._lock:
            record = self._records.get(key)
            return record is not None and record.locked_until > self._clock()

    def begin(self, key: str) -> bool:
        """Start an identity check; False (and nothing started) while `key` is locked out."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            record = self._records.setdefault(key, _FailureRecord())
            if record.locked_until > now:
                return False
            if len(record.failures) + record.in_flight >= self._max_failures:
                return False
            record.in_flight += 1
            return True

    def finish(self, key: str, *, failed: bool) -> bool:
        """End a check started with `begin`; returns True when its failure triggered a lockout."""
        with self._lock:
            now = self._clock()
            record = self._records[key]
            record.in_flight -= 1
            if not failed:
                return False
            self._expire_failures(record, now)
            record.failures.append(now)
            if len(record.failures) < self._max_failures:
                return False
            record.failures.clear()
            record.locked_until = now + self._window
            return True

    def _expire_failures(self, record: _FailureRecord, now: float) -> None:
        while record.failures and record.failures[0] <= now - self._window:
            record.failures.popleft()

    def _prune(self, now: float) -> None:
        for key, record in list(self._records.items()):
            self._expire_failures(record, now)
            if not record.failures and not record.in_flight and record.locked_until <= now:
                del self._records[key]


# --------------------------------------------------------------------------- audit


def mask_phone(phone: str) -> str:
    return "*" * max(len(phone) - 2, 0) + phone[-2:]


def mask_date_of_birth(dob: str) -> str:
    return dob[:4] + "-**-**"  # year only


def mask_name(name: str) -> str:
    return " ".join(f"{part[0]}." for part in name.split())


def mask_code(code: str) -> str:
    return "#" + _fingerprint(code)  # a confirmation code is half a credential


_MASKERS: dict[str, Callable[[str], str]] = {
    "phone": mask_phone,
    "date_of_birth": mask_date_of_birth,
    "patient_name": mask_name,
    "confirmation_code": mask_code,
}
_FREE_TEXT = frozenset({"query", "reason"})


def mask_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Audit-safe view of tool arguments: identifiers masked, free text reduced to a length."""
    masked: dict[str, Any] = {}
    for name, value in arguments.items():
        if value is None:
            continue
        if name in _MASKERS:
            masked[name] = _MASKERS[name](str(value))
        elif name in _FREE_TEXT:
            masked[name] = f"<{len(str(value))} chars>"
        else:
            masked[name] = value
    return masked


def write_audit_event(event: Mapping[str, Any]) -> None:
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **event}
    audit_logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


# --------------------------------------------------------------------------- guard


class ToolGuard:
    def __init__(self, *, limiter: RateLimiter, lockout: LockoutTracker) -> None:
        self.limiter = limiter
        self.lockout = lockout

    @contextmanager
    def call(
        self,
        tool: str,
        scope: str,
        ctx: Context | None,
        *,
        verifies_identity: bool = False,
        **arguments: Any,
    ) -> Iterator[Caller]:
        """Run a tool body under the guard. `arguments` are only used (masked) for auditing."""
        caller = current_caller(ctx)
        started = time.perf_counter()
        outcome = "ok"
        checking_identity = False
        try:
            if scope not in caller.scopes:
                outcome = "forbidden"
                raise ToolError(FORBIDDEN_MESSAGE)
            # In-process callers (tests, evals) are trusted and exempt from rate limiting.
            if caller.principal != LOCAL_PRINCIPAL and not self.limiter.allow(caller.principal):
                outcome = "rate_limited"
                raise ToolError(RATE_LIMITED_MESSAGE)
            if verifies_identity:
                if not self.lockout.begin(caller.key):
                    outcome = "locked_out"
                    raise ToolError(LOCKED_OUT_MESSAGE)
                checking_identity = True
            yield caller
        except IdentityVerificationError as exc:
            outcome = "identity_failed"
            raise ToolError(str(exc)) from None
        except ClinicError as exc:
            outcome = "rejected"
            raise ToolError(str(exc)) from None
        except ToolError:
            raise
        except Exception:
            outcome = "error"
            logger.exception("Tool %s failed unexpectedly", tool)
            raise ToolError(INTERNAL_ERROR_MESSAGE) from None
        finally:
            if checking_identity and self.lockout.finish(
                caller.key, failed=outcome == "identity_failed"
            ):
                logger.warning("Identity checks locked for client %s", caller.principal)
            write_audit_event(
                {
                    "event": "tool_call",
                    "tool": tool,
                    "client": caller.principal,
                    "session": _fingerprint(caller.session_id) if caller.session_id else None,
                    "outcome": outcome,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "args": mask_arguments(arguments),
                }
            )


def _fingerprint(value: str) -> str:
    """Short keyed hash: equal values match within this process, and the log alone can't be
    reversed by hashing every possible value (a confirmation code has under a million)."""
    return hmac.new(_FINGERPRINT_KEY, value.encode(), hashlib.sha256).hexdigest()[:12]
