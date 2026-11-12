"""A bot caller that joins a LiveKit room like a browser user and measures what a person hears.

All timings are taken on the caller's side of the WebRTC connection, so they include network
and jitter-buffer delay in both directions: the latency a caller actually experiences.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from livekit import rtc

from .audio import FRAME_MS, SAMPLE_RATE, SAMPLES_PER_FRAME, ActivityTracker, silence

logger = logging.getLogger("receptionist.evals.voice")

MIC_QUEUE_MS = 40
_SILENT_FRAME = silence(FRAME_MS)


@dataclass
class Utterance:
    started_at: float
    ended_at: float


@dataclass
class _Pending:
    chunks: deque[bytes]
    done: asyncio.Future[Utterance]
    started_at: float | None = None


@dataclass
class AgentTranscript:
    text: str
    at: float


@dataclass
class CallerLog:
    transcripts: list[AgentTranscript] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class VoiceCaller:
    def __init__(self, url: str, token: str, *, threshold_dbfs: float = -45.0) -> None:
        self._url = url
        self._token = token
        self.room = rtc.Room()
        self.agent_audio = ActivityTracker(threshold_dbfs=threshold_dbfs)
        self.log = CallerLog()
        self._source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=MIC_QUEUE_MS)
        self._queue: deque[_Pending] = deque()
        self._tasks: list[asyncio.Task[None]] = []
        self.agent_joined = asyncio.Event()

    async def __aenter__(self) -> VoiceCaller:
        self.room.on("track_subscribed", self._on_track_subscribed)
        self.room.on("data_received", self._on_data)
        self.room.register_text_stream_handler("lk.transcription", self._on_transcription)
        await self.room.connect(self._url, self._token)
        track = rtc.LocalAudioTrack.create_audio_track("caller-mic", self._source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(track, options)
        self._tasks.append(asyncio.create_task(self._mic_loop()))
        return self

    async def __aexit__(self, *exc: object) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.room.disconnect()

    # -- speaking ---------------------------------------------------------------------

    def start_speaking(self, pcm: bytes) -> asyncio.Future[Utterance]:
        """Queue speech on the mic; resolves when its last frame has been sent."""
        frame_bytes = SAMPLES_PER_FRAME * 2
        chunks = deque(
            pcm[i : i + frame_bytes].ljust(frame_bytes, b"\x00")
            for i in range(0, len(pcm), frame_bytes)
        )
        done: asyncio.Future[Utterance] = asyncio.get_running_loop().create_future()
        self._queue.append(_Pending(chunks=chunks, done=done))
        return done

    async def say(self, pcm: bytes) -> Utterance:
        return await self.start_speaking(pcm)

    async def _mic_loop(self) -> None:
        # A real microphone never stops sending frames; neither does this one.
        while True:
            pending = self._queue[0] if self._queue else None
            if pending is not None and pending.chunks:
                if pending.started_at is None:
                    pending.started_at = time.perf_counter() + MIC_QUEUE_MS / 1000
                chunk = pending.chunks.popleft()
            else:
                chunk = _SILENT_FRAME
            await self._source.capture_frame(
                rtc.AudioFrame(chunk, SAMPLE_RATE, 1, SAMPLES_PER_FRAME)
            )
            if pending is not None and not pending.chunks:
                self._queue.popleft()
                # the frame just captured leaves the queue ~MIC_QUEUE_MS from now
                ended = time.perf_counter() + MIC_QUEUE_MS / 1000
                if not pending.done.done():
                    pending.done.set_result(Utterance(pending.started_at or ended, ended))

    # -- listening --------------------------------------------------------------------

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if (
            track.kind != rtc.TrackKind.KIND_AUDIO
            or participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        ):
            return
        self.agent_joined.set()
        self._tasks.append(asyncio.create_task(self._listen(track)))

    async def _listen(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=FRAME_MS
        )
        async for event in stream:
            self.agent_audio.push(event.frame.data, time.perf_counter())

    def _on_data(self, packet: rtc.DataPacket) -> None:
        if packet.topic != "receptionist.events":
            return
        with contextlib.suppress(json.JSONDecodeError):
            self.log.events.append({**json.loads(packet.data), "_at": time.perf_counter()})

    def _on_transcription(self, reader: rtc.TextStreamReader, participant_identity: str) -> None:
        async def read() -> None:
            text = await reader.read_all()
            if participant_identity.startswith("agent") and text.strip():
                self.log.transcripts.append(AgentTranscript(text.strip(), time.perf_counter()))

        self._tasks.append(asyncio.create_task(read()))

    async def wait_for_agent_turn(
        self, *, after: float, timeout: float = 20.0, settle_s: float = 1.5
    ) -> tuple[float | None, float | None]:
        """Wait for the agent to start and finish replying after `after`.

        Returns (first audio, end of audio). The turn counts as finished once the agent has
        been silent for `settle_s`, so pauses between sentences don't end it early.
        """
        deadline = time.perf_counter() + timeout
        onset = None
        while time.perf_counter() < deadline:
            onset = onset or self.agent_audio.first_onset_after(after)
            if onset is not None and not self.agent_audio.speaking:
                last_end = self.agent_audio.segments[-1].end
                if last_end is not None and time.perf_counter() - last_end >= settle_s:
                    return onset, last_end
            await asyncio.sleep(0.02)
        return onset, None

    def agent_text_since(self, t: float) -> str:
        return " ".join(tr.text for tr in self.log.transcripts if tr.at >= t)
