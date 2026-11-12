from __future__ import annotations

import pytest

from receptionist.web.ratelimit import SlidingWindowLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_limit_then_reports_retry_after() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(3, 60, clock=clock)
    assert [limiter.hit("1.2.3.4") for _ in range(3)] == [None, None, None]
    clock.now += 10
    assert limiter.hit("1.2.3.4") == pytest.approx(50)
    # Other clients are unaffected.
    assert limiter.hit("5.6.7.8") is None


def test_window_slides() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, 10, clock=clock)
    limiter.hit("k")
    clock.now += 6
    limiter.hit("k")
    assert limiter.hit("k") is not None
    clock.now += 4.5  # the first event has aged out, the second hasn't
    assert limiter.hit("k") is None
    assert limiter.hit("k") is not None


def test_retry_after_does_not_consume() -> None:
    limiter = SlidingWindowLimiter(1, 10, clock=FakeClock())
    assert limiter.retry_after("k") is None
    assert limiter.retry_after("k") is None
    limiter.record("k")
    assert limiter.retry_after("k") is not None
    limiter.reset("k")
    assert limiter.retry_after("k") is None


def test_key_count_is_bounded() -> None:
    limiter = SlidingWindowLimiter(1, 60, max_keys=3, clock=FakeClock())
    for key in "abcd":
        limiter.hit(key)
    # "a" was evicted as the least recently seen key.
    assert limiter.hit("a") is None
    assert limiter.hit("d") is not None


def test_rejects_nonsense_configuration() -> None:
    with pytest.raises(ValueError):
        SlidingWindowLimiter(0, 60)
