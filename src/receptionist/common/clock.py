"""Clinic clock.

Every component (MCP server, agent prompt, billing agent, evals) reads "now" from here so a
single env var (`CLINIC_NOW`) can pin time for reproducible evaluation runs.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/New_York"

# Fixed clock used by the eval suite and the seeded fixtures (a Monday morning).
EVAL_NOW = "2026-11-02T09:00:00"


def clinic_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("CLINIC_TIMEZONE", DEFAULT_TIMEZONE))


def now() -> datetime:
    """Timezone-aware current time in the clinic's timezone.

    Honors `CLINIC_NOW` (ISO 8601, naive values are interpreted in the clinic timezone).
    """
    tz = clinic_tz()
    pinned = os.getenv("CLINIC_NOW")
    if pinned:
        dt = datetime.fromisoformat(pinned)
        return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
    return datetime.now(tz)


def today_str() -> str:
    """Human/LLM-friendly date line, e.g. 'Monday, November 2, 2026'."""
    n = now()
    return f"{n:%A}, {n:%B} {n.day}, {n.year}"
