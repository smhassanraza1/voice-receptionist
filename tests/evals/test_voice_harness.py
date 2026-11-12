"""Validates the voice harness's measurements against a synthetic agent with known timing.

A fake agent on a real local LiveKit server answers every caller utterance with a tone
REPLY_DELAY_S after the caller goes quiet, and stops talking REACTION_S after the caller
starts speaking over it. If the harness reports those numbers (plus a little WebRTC transport
delay), its TTFA and barge-in measurements can be trusted on real agents.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from livekit import api, rtc

from receptionist.evals.voice.audio import (
    FRAME_MS,
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    ActivityTracker,
    silence,
    tone,
)
from receptionist.evals.voice.caller import VoiceCaller
from receptionist.evals.voice.probes import Overlap, Probe, run_probes, summarize
from receptionist.evals.voice.stack import DEV_KEY, DEV_SECRET, LocalLiveKit, find_livekit_server

pytestmark = pytest.mark.skipif(
    find_livekit_server() is None, reason="livekit-server binary not available"
)

CALLER_HANGOVER_S = 0.3  # how long the fake agent waits to decide the caller is done
REPLY_DELAY_S = 0.4
REPLY_MS = 3000
REACTION_S = 0.1


class FakeAgent:
    def __init__(self, url: str, token: str) -> None:
        self.room = rtc.Room()
        self._url, self._token = url, token
        self._source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=40)
        self._caller = ActivityTracker(hangover_ms=int(CALLER_HANGOVER_S * 1000))
        self._tasks: list[asyncio.Task] = []
        self._speaking_until = 0.0
        self._reply_starts = 0.0
        self._barged = False

    async def __aenter__(self) -> FakeAgent:
        self.room.on("track_subscribed", self._on_track)
        await self.room.connect(self._url, self._token)
        track = rtc.LocalAudioTrack.create_audio_track("agent", self._source)
        await self.room.local_participant.publish_track(track)
        self._tasks.append(asyncio.create_task(self._speaker()))
        return self

    async def __aexit__(self, *exc: object) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.room.disconnect()

    def _on_track(self, track: rtc.Track, *_: object) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            self._tasks.append(asyncio.create_task(self._listen(track)))

    async def _listen(self, track: rtc.Track) -> None:
        handled_segments = 0
        async for ev in rtc.AudioStream(
            track, sample_rate=SAMPLE_RATE, num_channels=1, frame_size_ms=FRAME_MS
        ):
            now = time.perf_counter()
            self._caller.push(ev.frame.data, now)
            if (
                self._caller.speaking
                and self._reply_starts <= now < self._speaking_until
                and not self._barged
            ):
                self._barged = True
                self._speaking_until = now + REACTION_S  # barge-in: stop shortly
            segs = self._caller.segments
            if len(segs) > handled_segments and segs[-1].end is not None:
                handled_segments = len(segs)
                start = now + REPLY_DELAY_S
                self._speaking_until = start + REPLY_MS / 1000
                self._reply_starts = start
                self._barged = False

    async def _speaker(self) -> None:
        self._reply_starts = time.perf_counter() + 0.5  # greeting
        self._speaking_until = self._reply_starts + 1.0
        loud = tone(FRAME_MS, freq=330)
        quiet = silence(FRAME_MS)
        while True:
            now = time.perf_counter()
            chunk = loud if self._reply_starts <= now < self._speaking_until else quiet
            await self._source.capture_frame(
                rtc.AudioFrame(chunk, SAMPLE_RATE, 1, SAMPLES_PER_FRAME)
            )


async def test_harness_measures_known_latencies(tmp_path):
    with LocalLiveKit(tmp_path) as lk:
        room = lk.new_room()
        agent_token = (
            api.AccessToken(DEV_KEY, DEV_SECRET)
            .with_identity("agent-fake")
            .with_kind("agent")
            .with_grants(api.VideoGrants(room_join=True, room=room, agent=True))
            .to_jwt()
        )
        probes = [
            Probe(id="plain", say="one"),
            Probe(id="barge", say="two", interrupt=Overlap(after_ms=800, say="three")),
            Probe(id="plain-2", say="four"),
        ]
        async with (
            FakeAgent(lk.url, agent_token),
            VoiceCaller(lk.url, lk.token("caller", room)) as caller,
        ):
            run = await run_probes(
                caller, probes, arch="fake", speech=lambda _: tone(700), turn_timeout=10
            )

    by_id = {r.probe_id: r for r in run.results}
    expected_ttfa_ms = (CALLER_HANGOVER_S + REPLY_DELAY_S) * 1000
    for probe_id in ("plain", "plain-2"):
        ttfa = by_id[probe_id].ttfa_ms
        assert ttfa is not None
        # transport + onset detection adds tens of ms on localhost; never subtracts
        assert expected_ttfa_ms - 30 <= ttfa <= expected_ttfa_ms + 250, ttfa

    barge = by_id["barge"]
    assert barge.interrupted is True
    assert barge.stop_latency_ms is not None
    assert REACTION_S * 1000 <= barge.stop_latency_ms <= REACTION_S * 1000 + 300, (
        barge.stop_latency_ms
    )
    assert barge.reply_ttfa_ms is not None

    summary = summarize([run])
    assert summary["barge_in"]["stop_rate"] == 1.0
    assert summary["ttfa_ms"]["n"] == 3
