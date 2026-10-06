"""The rate gate: one slot per 1/rate, a shared pause, waits that end on a stop request."""

from __future__ import annotations

from datetime import timedelta

import pytest

from wortly_telegram.delivery.capacity import TelegramCapacity


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.waits: list[float] = []
        self.stopping = False

    def monotonic(self) -> float:
        return self.now

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        if self.stopping:
            return True
        self.now += seconds
        return False


@pytest.fixture
def clock() -> FakeTime:
    return FakeTime()


def gate(clock: FakeTime, rate: int = 4) -> TelegramCapacity:
    return TelegramCapacity(rate, wait=clock.wait, monotonic=clock.monotonic)


def test_slots_are_spaced_by_one_over_the_rate_and_never_wait_longer(clock):
    capacity = gate(clock)
    capacity.wait_for_slot()
    assert clock.waits == []
    capacity.wait_for_slot()
    capacity.wait_for_slot()
    assert clock.waits == [0.25, 0.25]
    assert max(clock.waits) <= capacity.slot


def test_an_idle_gate_gives_the_next_slot_at_once(clock):
    capacity = gate(clock)
    capacity.wait_for_slot()
    clock.now += 10
    capacity.wait_for_slot()
    assert clock.waits == []


def test_a_pause_closes_the_gate_until_it_ends(clock):
    capacity = gate(clock)
    capacity.pause(timedelta(seconds=30))
    assert capacity.paused_for() == 30
    capacity.wait_until_open()
    assert sum(clock.waits) == pytest.approx(30)
    assert capacity.paused_for() == 0


def test_a_shorter_pause_does_not_shorten_a_longer_one(clock):
    capacity = gate(clock)
    capacity.pause(timedelta(seconds=30))
    capacity.pause(timedelta(seconds=5))
    assert capacity.paused_for() == 30


def test_an_open_gate_does_not_wait(clock):
    capacity = gate(clock)
    capacity.wait_until_open()
    assert clock.waits == []


def test_a_stop_request_ends_the_pause_wait(clock):
    capacity = gate(clock)
    capacity.pause(timedelta(hours=10))
    clock.stopping = True
    capacity.wait_until_open()
    assert len(clock.waits) == 1


@pytest.mark.parametrize("rate", [0, -1, 1.5, True])
def test_the_rate_is_a_positive_integer(clock, rate):
    with pytest.raises(ValueError):
        TelegramCapacity(rate, wait=clock.wait, monotonic=clock.monotonic)


def test_a_negative_pause_is_refused(clock):
    with pytest.raises(ValueError):
        gate(clock).pause(timedelta(seconds=-1))
