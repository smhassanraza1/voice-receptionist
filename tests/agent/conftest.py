import sys
from pathlib import Path

import pytest

from receptionist.common.clock import EVAL_NOW

# reuse the scripted LLM from the eval tests
sys.path.insert(0, str(Path(__file__).parents[1] / "evals"))


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLINIC_NOW", EVAL_NOW)
    for key in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "CARTESIA_API_KEY",
        "ELEVEN_API_KEY",
    ):
        monkeypatch.setenv(key, "test-key")
    monkeypatch.setenv("BILLING_AGENT_LLM", "off")
