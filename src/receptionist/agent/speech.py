"""Text rewrites applied before TTS (pipeline only: speech-to-speech models have no text step).

Confirmation codes are the one thing a caller must hear exactly. Prompting the LLM to spell
them works most of the time; rewriting the text right before synthesis works every time.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterable

CODE_RE = re.compile(r"\bLKC-([A-Z0-9]{4})\b")
# a code that may still be arriving at the end of the buffer ("L", "LK", ... "LKC-7Q2")
PARTIAL_CODE_RE = re.compile(r"\bL(?:K(?:C(?:-[A-Z0-9]{0,3})?)?)?\Z")


def _spell(match: re.Match[str]) -> str:
    return "L K C, " + " ".join(match.group(1))


def spell_codes(text: str) -> str:
    return CODE_RE.sub(_spell, text)


async def spell_confirmation_codes(chunks: AsyncIterable[str]) -> AsyncIterable[str]:
    """Streaming transform: holds back only a possible partial code at the end of the buffer."""
    buffer = ""
    async for chunk in chunks:
        buffer = spell_codes(buffer + chunk)
        partial = PARTIAL_CODE_RE.search(buffer)
        cut = partial.start() if partial else len(buffer)
        if cut:
            yield buffer[:cut]
        buffer = buffer[cut:]
    if buffer:
        yield spell_codes(buffer)
