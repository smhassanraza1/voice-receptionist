"""LLM-driven simulated callers."""

from __future__ import annotations

import random
import re

from livekit.agents import llm

from .llm_utils import complete
from .scenarios import Scenario

END_TOKEN = "<END_CALL>"

_SYSTEM = """\
You are role-playing a person phoning a medical clinic's virtual receptionist. Stay in
character for the whole call. You are the CALLER, never the receptionist.

Persona: {name}. {traits}
Speak {language_name}.
What you want: {goal}
Things you know (share only when asked, the way a real caller would): {facts}

Rules:
- Say one short, natural spoken turn (usually under 25 words). No stage directions.
- Do not volunteer everything at once; answer what you are asked.
- If the receptionist asks you to confirm something correct, confirm it. If something is
  wrong, correct it.
- When your goal is done (or clearly cannot be done) and you have said goodbye, or the
  receptionist has said goodbye, reply with exactly {end}.
"""

LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
    "ja": "Japanese",
}


def add_asr_noise(text: str, rate: float, rng: random.Random) -> str:
    """Mimic speech-recognition errors: dropped words, merged words, letter slips."""
    if rate <= 0:
        return text
    words = text.split()
    out: list[str] = []
    for word in words:
        r = rng.random()
        if r < rate * 0.3 and len(words) > 4:
            continue  # dropped word
        if r < rate * 0.7 and len(word) > 3:
            i = rng.randrange(1, len(word) - 1)
            word = word[:i] + word[i + 1 :]  # missing letter
        elif r < rate and out:
            out[-1] = out[-1] + word.lower()  # merged words
            continue
        out.append(word)
    return " ".join(out)


class CallerSimulator:
    def __init__(self, model: llm.LLM, scenario: Scenario, *, seed: int = 7) -> None:
        self._model = model
        self._scenario = scenario
        self._rng = random.Random(seed)
        persona = scenario.persona
        self._system = _SYSTEM.format(
            name=persona.name,
            traits=persona.traits,
            language_name=LANGUAGE_NAMES.get(persona.language, "English"),
            goal=scenario.goal.strip(),
            facts="; ".join(f"{k}: {v}" for k, v in persona.facts.items()) or "nothing special",
            end=END_TOKEN,
        )

    async def next_turn(self, transcript: str) -> str | None:
        """The caller's next utterance, or None when the caller hangs up."""
        prompt = (
            f"Conversation so far:\n{transcript or '(the call just connected)'}\n\n"
            "Your next line as the caller:"
        )
        text = await complete(self._model, system=self._system, user=prompt, temperature=0.8)
        text = re.sub(r"^(CALLER|Caller)\s*:\s*", "", text).strip().strip('"')
        if not text or END_TOKEN in text:
            return None
        return add_asr_noise(text, self._scenario.persona.asr_noise, self._rng)
