import pytest

from receptionist.evals.voice.audio import ActivityTracker, frame_dbfs, silence, tone


def test_activity_tracker_segments_with_hangover():
    tracker = ActivityTracker(hangover_ms=100)
    t = 0.0
    for pcm in (
        [silence(10)] * 5
        + [tone(10)] * 20
        + [silence(10)] * 5
        + [tone(10)] * 5
        + [silence(10)] * 30
    ):
        t += 0.01
        tracker.push(pcm, t)
    # the 50 ms gap is shorter than the hangover, so it is one segment
    assert len(tracker.segments) == 1
    seg = tracker.segments[0]
    assert seg.start == pytest.approx(0.05, abs=0.011)
    assert seg.end == pytest.approx(0.35, abs=0.011)
    assert not tracker.speaking


def test_frame_dbfs():
    assert frame_dbfs(silence(10)) == -120.0
    assert -14 < frame_dbfs(tone(10, amplitude=0.3)) < -12  # sine RMS = peak / sqrt(2)
