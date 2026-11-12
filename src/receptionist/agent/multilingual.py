"""Follows the caller's language.

Speech-to-speech models switch languages on their own. In the pipeline, Deepgram's `multi`
model reports a language per final transcript, and the TTS has to be told to switch: otherwise
an English voice reads Spanish text with an English accent.
"""

from __future__ import annotations

import logging

from livekit.agents import AgentSession
from livekit.agents.voice.events import UserInputTranscribedEvent

from .settings import SUPPORTED_LANGUAGES
from .state import CallState

logger = logging.getLogger("receptionist.agent")

MIN_WORDS_TO_SWITCH = 3  # short utterances ("ok", "sí") are too ambiguous to switch on


class LanguageFollower:
    def __init__(self, session: AgentSession[CallState], *, switch_tts: bool) -> None:
        self._session = session
        self._switch_tts = switch_tts

    def attach(self) -> None:
        self._session.on("user_input_transcribed", self._on_transcript)

    def _on_transcript(self, ev: UserInputTranscribedEvent) -> None:
        if not ev.is_final or ev.language is None:
            return
        language = ev.language.language
        state = self._session.userdata
        if (
            language == state.language
            or language not in SUPPORTED_LANGUAGES
            or len(ev.transcript.split()) < MIN_WORDS_TO_SWITCH
        ):
            return

        logger.info("caller language changed", extra={"from": state.language, "to": language})
        state.language = language
        tts = self._session.tts
        if self._switch_tts and tts is not None and hasattr(tts, "update_options"):
            tts.update_options(language=language)
