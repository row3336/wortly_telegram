"""The delivery process's rate gate for Telegram (design T-D §3.5, К12-005…007).

A token bucket of one token refilled ``messages_per_second`` times a second, plus a
pause shared by the whole gate. The core's delivery loop (wortly.jobs.loop) calls

- ``wait_until_open`` while it holds no claim: it waits out a pause, which has no
  upper bound because Telegram's retry_after has none (ВП-25);
- ``wait_for_slot`` after the claim and before the preflight: the wait is at most one
  slot, 1 / messages_per_second, so the job lease covers it (§2.2).

The channel calls ``pause`` when Telegram answers 429 with retry_after; the next claim
waits until then. Nothing here waits after the sending decision: there is no
AIORateLimiter in this process, it would wait inside the call (TC:282). The gate lives
in memory; several delivery processes share the limit through their configured rates
(ВП-7). Waiting goes through ``wait(seconds)``, which returns early once the process is
asked to stop, so a long pause never delays a shutdown.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import threading
import time


# Event.wait refuses longer timeouts; a longer pause is waited out in several steps.
_LONGEST_WAIT = threading.TIMEOUT_MAX


class TelegramCapacity:
    def __init__(self, messages_per_second: int, *, wait: Callable[[float], object],
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if type(messages_per_second) is not int or messages_per_second < 1:
            raise ValueError("messages_per_second must be a positive integer")
        self._slot = 1.0 / messages_per_second
        self._wait = wait
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._paused_until = float("-inf")
        self._next_slot = float("-inf")

    @property
    def slot(self) -> float:
        """Seconds between two sends; the longest wait_for_slot."""
        return self._slot

    def paused_for(self) -> float:
        """Seconds left of the current pause, 0.0 when the gate is open."""
        with self._lock:
            return max(0.0, self._paused_until - self._monotonic())

    def pause(self, duration: timedelta) -> None:
        """Close the gate for ``duration`` from now; a longer pause already set is kept."""
        if type(duration) is not timedelta or duration < timedelta(0):
            raise ValueError("A pause needs a non-negative duration")
        with self._lock:
            self._paused_until = max(self._paused_until, self._monotonic() + duration.total_seconds())

    def wait_until_open(self) -> None:
        """Block while the gate is paused (no claim is held while this waits)."""
        while (left := self.paused_for()) > 0:
            if self._wait(min(left, _LONGEST_WAIT)):
                return

    def wait_for_slot(self) -> None:
        """Take the next send slot, waiting at most one slot for it."""
        with self._lock:
            now = self._monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._slot
        if slot > now:
            self._wait(slot - now)
