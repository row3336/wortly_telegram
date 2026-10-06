"""The single poller of the bot role: a leader loop over the core's lease (design T-D §2.4.1, ВП-5а).

Telegram does not guarantee one consumer of getUpdates (TC:291), so only the holder
of the "bot" lease polls. The lease lives in the core's PostgreSQL (wortly.jobs.leases);
the bot reaches it only through the core's internal API (gateway.HttpLeaseStore,
answer 2.4). The rules of this loop:

- Time is this process's monotonic clock, never the wall clock. A lease taken or
  renewed by a call that started at local time ``t`` (read before the request is sent)
  expires no earlier than ``t + L`` in the database, because the core starts the
  duration inside its own transaction, after the request arrived. Polling must
  therefore have stopped by ``t + L - margin``; the core's expires_at is never used.
- A lease whose acquisition answered only after that deadline is not used for polling
  at all: it is given back at once (a slow core, a long network hop).
- The lease is renewed every L/3. A renewal that fails in any way (the lease is lost,
  the database is gone, anything raised) loses the leadership at once: polling stops.
- A renewal that has not answered by the deadline loses the leadership too. The
  deadline is waited for beside the call, which is then cancelled, so a hanging core
  never keeps polling alive past it. Every store call also has its own deadline
  (gateway.InternalClient), shorter than the margin (config.py).
- Stopping polling ends with one more getUpdates that confirms the fetched updates
  (PTB Updater._stop_polling); the margin covers that request (config.py checks it).
- A standby instance tries to take the lease every ``standby_retry``.
- On a stop request polling stops first and the lease is released after, so a
  successor need not wait for it to expire (design §2.4.4).

Store calls are coroutines (HTTP requests); nothing here imports a database driver.
Logs carry exception classes only (TC:273).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any, Protocol


logger = logging.getLogger(__name__)

ROLE = "bot"


@dataclass(frozen=True)
class LeaderConfig:
    """``lease`` is L of design §2.2; every value comes from the configuration."""

    holder: str
    lease: timedelta
    stop_margin: timedelta
    standby_retry: timedelta

    def __post_init__(self) -> None:
        if type(self.holder) is not str or not self.holder.strip() or self.holder != self.holder.strip():
            raise ValueError("bot instance must be a non-empty name without surrounding blanks")
        for name in ("lease", "stop_margin", "standby_retry"):
            value = getattr(self, name)
            if type(value) is not timedelta or value <= timedelta(0):
                raise ValueError(f"bot {name} must be a positive duration")
        if self.stop_margin >= self.lease - self.renew_period:
            # Otherwise the deadline passes before the first renewal is even tried.
            raise ValueError("bot stop_margin_seconds must be shorter than two thirds of leader_lease_seconds")

    @property
    def renew_period(self) -> timedelta:
        return self.lease / 3


class Poller(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class LeaseStore(Protocol):
    """The core's lease of a role (gateway.HttpLeaseStore); acquire answers None while held."""

    async def acquire(self, role: str, *, holder: str, duration: timedelta) -> Any: ...

    async def renew(self, lease: Any, *, duration: timedelta) -> Any: ...

    async def release(self, lease: Any) -> bool: ...


class Leader:
    def __init__(self, store: LeaseStore, poller: Poller, *, config: LeaderConfig,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._store = store
        self._poller = poller
        self._config = config
        self._monotonic = monotonic
        self._sleep = sleep
        self._leading = False

    @property
    def leading(self) -> bool:
        """True while this process holds the lease and polls (bot readiness, design §2.3)."""
        return self._leading

    def _seconds(self, value: timedelta) -> float:
        return value.total_seconds()

    async def _pause(self, stop: asyncio.Event, seconds: float) -> bool:
        """Wait ``seconds`` or until a stop request; True when stopping."""
        if stop.is_set():
            return True
        if seconds > 0:
            stopping = asyncio.ensure_future(stop.wait())
            waiting = asyncio.ensure_future(self._sleep(seconds))
            try:
                await asyncio.wait({stopping, waiting}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopping.cancel()
                waiting.cancel()
        return stop.is_set()

    async def run(self, stop: asyncio.Event) -> None:
        """Take, hold and give back the lease until ``stop`` is set."""
        while not stop.is_set():
            started = self._monotonic()
            try:
                lease = await self._store.acquire(ROLE, holder=self._config.holder,
                                                  duration=self._config.lease)
            except Exception as error:  # noqa: BLE001 - a standby retries later
                logger.warning("bot lease acquire failed: %s", type(error).__name__)
                lease = None
            if lease is None:
                await self._pause(stop, self._seconds(self._config.standby_retry))
                continue
            if self._monotonic() >= self._deadline(started):
                # The answer came too late to poll safely under this lease.
                logger.warning("bot lease answered after its deadline; given back")
                held = lease
            else:
                logger.info("bot lease taken; polling starts")
                held = await self._lead(lease, started, stop)
            if held is not None:
                await self._release(held)
            # After a lost lease or a failed start, stand by before trying again.
            await self._pause(stop, self._seconds(self._config.standby_retry))

    async def _lead(self, lease: Any, started: float, stop: asyncio.Event) -> Any:
        """Poll while the lease holds; return the lease if still held, None once lost."""
        try:
            await self._poller.start()
        except Exception as error:  # noqa: BLE001 - give the lease back and stand by
            logger.warning("bot polling did not start: %s", type(error).__name__)
            return lease
        self._leading = True
        try:
            return await self._hold(lease, started, stop)
        finally:
            self._leading = False
            try:
                await self._poller.stop()
            finally:
                logger.info("bot polling stopped")

    def _deadline(self, started: float) -> float:
        return started + self._seconds(self._config.lease - self._config.stop_margin)

    async def _hold(self, lease: Any, started: float, stop: asyncio.Event) -> Any:
        deadline = self._deadline(started)
        period = self._seconds(self._config.renew_period)
        while True:
            if await self._pause(stop, started + period - self._monotonic()):
                return lease
            attempt = self._monotonic()
            if attempt >= deadline:
                logger.warning("bot lease deadline passed before renewal; polling stops")
                return None
            renewed = await self._renew_before(lease, deadline)
            if renewed is None:
                return None
            lease, started, deadline = renewed, attempt, self._deadline(attempt)

    async def _renew_before(self, lease: Any, deadline: float) -> Any:
        renewal = asyncio.ensure_future(self._store.renew(lease, duration=self._config.lease))
        timer = asyncio.ensure_future(self._sleep(max(0.0, deadline - self._monotonic())))
        try:
            await asyncio.wait({renewal, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        if not renewal.done():
            # Its late answer no longer matters; the request is abandoned.
            renewal.cancel()
            renewal.add_done_callback(_discard)
            logger.warning("bot lease renewal did not answer before the deadline; polling stops")
            return None
        try:
            return renewal.result()
        except Exception as error:  # noqa: BLE001 - any failure of renew is a lost lease
            logger.warning("bot lease lost: %s", type(error).__name__)
            return None

    async def _release(self, lease: Any) -> None:
        try:
            await self._store.release(lease)
        except Exception as error:  # noqa: BLE001 - the lease then simply expires
            logger.warning("bot lease release failed: %s", type(error).__name__)


def _discard(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()
