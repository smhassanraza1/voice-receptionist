"""Audio helpers for the voice harness: synthesis (cached), tones, and energy-based activity."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 24_000
FRAME_MS = 10
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000
CACHE_DIR = Path(os.getenv("EVAL_AUDIO_CACHE", ".cache/eval-audio"))


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


def tone(ms: int, freq: float = 440.0, amplitude: float = 0.3) -> bytes:
    """A sine tone; stands in for speech when validating the harness itself."""
    t = np.arange(SAMPLE_RATE * ms // 1000) / SAMPLE_RATE
    return (amplitude * 32767 * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def frame_dbfs(pcm: bytes | memoryview) -> float:
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return -120.0
    mean_square = float(np.mean(samples * samples))
    return -120.0 if mean_square <= 0 else 10 * math.log10(mean_square / 32768.0**2)


@dataclass
class Segment:
    start: float
    end: float | None = None


class ActivityTracker:
    """Speech/silence segmentation from frame energy, with onset and hangover hysteresis.

    The agent's audio track carries digital silence between replies, so a fixed dBFS
    threshold separates speech cleanly; hysteresis avoids splitting on short pauses.
    """

    def __init__(
        self, threshold_dbfs: float = -45.0, onset_ms: int = 20, hangover_ms: int = 350
    ) -> None:
        self.threshold = threshold_dbfs
        self.onset_frames = max(1, onset_ms // FRAME_MS)
        self.hangover_s = hangover_ms / 1000
        self.segments: list[Segment] = []
        self._loud_run = 0
        self._first_loud_at = 0.0
        self._last_loud_at: float | None = None

    @property
    def speaking(self) -> bool:
        return bool(self.segments) and self.segments[-1].end is None

    def push(self, pcm: bytes | memoryview, at: float, frame_s: float = FRAME_MS / 1000) -> None:
        loud = frame_dbfs(pcm) > self.threshold
        if loud:
            if self._loud_run == 0:
                self._first_loud_at = at - frame_s
            self._loud_run += 1
            self._last_loud_at = at
            if not self.speaking and self._loud_run >= self.onset_frames:
                self.segments.append(Segment(start=self._first_loud_at))
        else:
            self._loud_run = 0
            if (
                self.speaking
                and self._last_loud_at is not None
                and at - self._last_loud_at >= self.hangover_s
            ):
                self.segments[-1].end = self._last_loud_at

    def first_onset_after(self, t: float) -> float | None:
        return next((s.start for s in self.segments if s.start >= t), None)

    def first_offset_after(self, t: float) -> float | None:
        return next((s.end for s in self.segments if s.end is not None and s.end >= t), None)


def synthesize(text: str, *, voice: str = "alloy") -> bytes:
    """24 kHz mono s16le speech for a caller line, cached on disk by content hash."""
    model = os.getenv("EVAL_TTS_MODEL", "gpt-4o-mini-tts")
    key = hashlib.sha256(f"{model}|{voice}|{text}".encode()).hexdigest()[:24]
    path = CACHE_DIR / f"{key}.pcm"
    if path.exists():
        return path.read_bytes()

    from openai import OpenAI

    response = OpenAI().audio.speech.create(
        model=model, voice=voice, input=text, response_format="pcm"
    )
    pcm = response.read()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm)
    return pcm
