"""The poller's lease on fake monotonic time: polling ends before the lease can expire (design T-D §2.2, §2.4.1).

The first tests script the store itself; the last ones run the real HttpLeaseStore over
FakeCore with a slow transport (answer 2.4): the core computes the lease when the request
arrives, the answer reaches the bot seconds later.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import pytest

from fakes.core_gateway import FakeCore, core_settings, forbid_network
from wortly_telegram.gateway import build_gateway
from wortly_telegram.leader import ROLE, Leader, LeaderConfig


L, MARGIN, STANDBY, STOP_TAKES = 60.0, 20.0, 10.0, 5.0
CONFIG = LeaderConfig(holder="bot-1", lease=timedelta(seconds=L), stop_margin=timedelta(seconds=MARGIN),
                      standby_retry=timedelta(seconds=STANDBY))


class Clock:
    """Monotonic time that moves only when the leader sleeps or the poller stops."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # Let every task that is ready finish first; a sleep cancelled meanwhile (the
        # race it was part of is decided) never moves the clock.
        for _ in range(5):
            await asyncio.sleep(0)
        self.now += seconds


@dataclass
class Lease:
    generation: int
    # The latest the database lets it expire, in this process's monotonic time: the
    # database computes "now + L" no earlier than the local start of the call.
    expires_at: float


class Store:
    """LeaseStore with a script per call.

    renew answers "ok", "hang", an exception, or a number of seconds the call takes. A slow
    call (and a slow acquire) gets the lease the database computed at the start of the call,
    while the answer reaches the leader that many seconds later.
    """

    def __init__(self, clock: Clock, *, renewals=(), grants=1, stop: asyncio.Event,
                 acquire_takes: float = 0.0) -> None:
        self.clock = clock
        self.acquire_takes = acquire_takes
        self.renewals = list(renewals)
        self.grants = grants
        self.stop = stop
        self.acquired: list[float] = []
        self.released: list[Lease] = []
        self.leases: list[Lease] = []
        self.hang = asyncio.Event()

    async def acquire(self, role, *, holder, duration):
        assert (role, holder, duration) == (ROLE, "bot-1", timedelta(seconds=L))
        self.acquired.append(self.clock.now)
        if len(self.acquired) > self.grants:
            # Ends the test run: every lease granted by the script was used.
            self.stop.set()
            return None
        lease = self._lease()
        self.clock.now += self.acquire_takes
        return lease

    def _lease(self) -> Lease:
        lease = Lease(len(self.leases) + 1, self.clock.now + L)
        self.leases.append(lease)
        return lease

    async def renew(self, lease, *, duration):
        answer = self.renewals.pop(0) if self.renewals else "ok"
        if answer == "hang":
            return await self._late()
        if isinstance(answer, BaseException):
            raise answer
        lease = self._lease()
        if isinstance(answer, float):
            self.clock.now += answer
        return lease

    async def _late(self):
        await self.hang.wait()
        return self._lease()

    async def release(self, lease) -> bool:
        self.released.append(lease)
        return True


class Poller:
    def __init__(self, clock: Clock, *, fail_start: bool = False) -> None:
        self.clock = clock
        self.fail_start = fail_start
        self.started: list[float] = []
        self.stopped: list[float] = []

    async def start(self) -> None:
        self.started.append(self.clock.now)
        if self.fail_start:
            raise RuntimeError("deleteWebhook failed")

    async def stop(self) -> None:
        # The final getUpdates that confirms fetched updates takes time too.
        self.clock.now += STOP_TAKES
        self.stopped.append(self.clock.now)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


async def run_leader(store, poller: Poller, clock: Clock, stop: asyncio.Event) -> Leader:
    leader = Leader(store, poller, config=CONFIG, monotonic=clock.monotonic, sleep=clock.sleep)
    try:
        await asyncio.wait_for(leader.run(stop), timeout=10)
    finally:
        if isinstance(store, Store):
            store.hang.set()
        await asyncio.sleep(0)
    return leader


def assert_polling_ended_in_time(store: Store, poller: Poller) -> None:
    # Every polling period ended (final getUpdates included) before the lease it ran
    # under could expire: no moment with two pollers (TC:291).
    assert len(poller.started) == len(poller.stopped)
    for stopped in poller.stopped:
        held = [lease for lease in store.leases if lease.expires_at - L <= stopped]
        assert stopped < max(lease.expires_at for lease in held)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.mark.parametrize("failure", [RuntimeError("lost"), OSError("connection reset"), LookupError("x"),
                                     TimeoutError()], ids=["lost-leadership", "database-gone", "other", "timeout"])
async def test_any_failed_renewal_stops_polling_at_once(clock, failure):
    stop = asyncio.Event()
    store, poller = Store(clock, renewals=[failure], stop=stop), Poller(clock)
    start = clock.now
    await run_leader(store, poller, clock, stop)
    assert poller.started == [start]
    # Stopped right after the failed renewal at L/3, not at the lease's end.
    assert poller.stopped == [start + L / 3 + STOP_TAKES]
    assert_polling_ended_in_time(store, poller)
    assert store.released == []


async def test_a_hanging_renewal_stops_polling_by_the_deadline(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, renewals=["hang"], stop=stop), Poller(clock)
    start = clock.now
    await run_leader(store, poller, clock, stop)
    assert poller.stopped == [start + L - MARGIN + STOP_TAKES]
    assert poller.stopped[0] < store.leases[0].expires_at
    assert_polling_ended_in_time(store, poller)


async def test_renewals_extend_the_deadline_from_the_start_of_each_call(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, renewals=["ok", "ok", "hang"], stop=stop), Poller(clock)
    start = clock.now
    await run_leader(store, poller, clock, stop)
    last_renewal = start + 2 * L / 3
    assert poller.stopped == [last_renewal + L - MARGIN + STOP_TAKES]
    assert poller.stopped[0] < store.leases[-1].expires_at
    assert_polling_ended_in_time(store, poller)


async def test_a_slow_renewal_counts_its_deadline_from_the_start_of_the_call(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, renewals=[15.0, "hang"], stop=stop), Poller(clock)
    start = clock.now
    await run_leader(store, poller, clock, stop)
    # The first renewal started at L/3 and answered 15 s later; the database counted its L
    # from the start, so the next deadline is that start + L - margin, not the answer's.
    first_renewal = start + L / 3
    assert poller.stopped == [first_renewal + L - MARGIN + STOP_TAKES]
    assert poller.stopped[0] < store.leases[1].expires_at
    assert_polling_ended_in_time(store, poller)


async def test_a_slow_acquire_counts_its_deadline_from_the_start_of_the_call(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, renewals=["hang"], stop=stop, acquire_takes=15.0), Poller(clock)
    start = clock.now
    await run_leader(store, poller, clock, stop)
    assert poller.started == [start + 15.0]
    assert poller.stopped == [start + L - MARGIN + STOP_TAKES]
    assert poller.stopped[0] < store.leases[0].expires_at
    assert_polling_ended_in_time(store, poller)


async def test_a_standby_never_polls_and_retries_after_the_standby_period(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, grants=0, stop=stop), Poller(clock)
    store.grants = -1  # every acquire answers None until the third, which stops the run

    async def acquire(role, *, holder, duration):
        store.acquired.append(clock.now)
        if len(store.acquired) == 3:
            stop.set()
        return None

    store.acquire = acquire
    start = clock.now
    leader = await run_leader(store, poller, clock, stop)
    assert poller.started == []
    assert store.acquired == [start, start + STANDBY, start + 2 * STANDBY]
    assert leader.leading is False


async def test_a_stop_request_stops_polling_then_releases_the_lease(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, stop=stop), Poller(clock)
    leader = Leader(store, poller, config=CONFIG, monotonic=clock.monotonic, sleep=clock.sleep)
    task = asyncio.create_task(leader.run(stop))
    for _ in range(50):
        await asyncio.sleep(0)
        if leader.leading:
            break
    assert leader.leading is True
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(poller.stopped) == 1
    assert store.released == [store.leases[-1]]
    assert leader.leading is False


async def test_a_failed_poller_start_gives_the_lease_back_and_stands_by(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, stop=stop), Poller(clock, fail_start=True)
    start = clock.now
    await run_leader(store, poller, clock, stop)
    assert store.released == [store.leases[0]]
    assert store.acquired == [start, start + STANDBY]


async def test_a_failing_acquire_is_a_standby(clock):
    stop = asyncio.Event()
    store, poller = Store(clock, stop=stop), Poller(clock)
    calls = []

    async def acquire(role, *, holder, duration):
        calls.append(clock.now)
        if len(calls) == 2:
            stop.set()
        raise ConnectionError("database unavailable")

    store.acquire = acquire
    await run_leader(store, poller, clock, stop)
    assert poller.started == []
    assert len(calls) == 2


async def test_an_acquire_answered_after_the_deadline_is_given_back_without_polling(clock):
    stop = asyncio.Event()
    store = Store(clock, stop=stop, acquire_takes=L - MARGIN)
    poller = Poller(clock)
    await run_leader(store, poller, clock, stop)
    assert poller.started == []
    assert store.released == [store.leases[0]]


# -- the real HttpLeaseStore over a slow core ----------------------------------------------

class SlowCore(FakeCore):
    """FakeCore on the fake clock whose lease answers take a scripted time per call."""

    def __init__(self, clock: Clock, *, takes=(), stop: asyncio.Event, acquisitions: int = 1) -> None:
        super().__init__(now=clock.monotonic, delay=self._take)
        self.clock = clock
        self.takes = list(takes)
        self.stop = stop
        self.acquisitions = acquisitions
        self.leases: list[float] = []
        self.hang = asyncio.Event()

    async def _take(self, action: str) -> None:
        if action == "acquire":
            self.acquisitions -= 1
            if self.acquisitions < 0:
                self.stop.set()
        if action in ("acquire", "renew") and self.lease is not None:
            self.leases.append(self.lease.expires_at)
        take = self.takes.pop(0) if self.takes and action != "release" else 0.0
        if take == "hang":
            await self.hang.wait()
        else:
            self.clock.now += take


def polling_ended_before_every_lease_expired(core: SlowCore, poller: Poller) -> bool:
    # Each polling period ended (final getUpdates included) before the core let the lease
    # it ran under expire: the core's own expiry, computed on arrival.
    return all(any(start < expiry and stopped < expiry for expiry in core.leases)
               for start, stopped in zip(poller.started, poller.stopped))


async def run_over_http(core: SlowCore, poller: Poller, clock: Clock, stop: asyncio.Event) -> Leader:
    gateway = build_gateway(core_settings(), transport=core.transport())
    try:
        return await run_leader(gateway.leases, poller, clock, stop)
    finally:
        core.hang.set()
        await gateway.aclose()


async def test_over_a_slow_core_the_deadline_counts_from_the_sending_of_each_request(clock):
    stop = asyncio.Event()
    # acquire 15 s, then renewals of 12 s and 15 s, then a hang.
    core, poller = SlowCore(clock, takes=[15.0, 12.0, 15.0, "hang"], stop=stop), Poller(clock)
    start = clock.now
    await run_over_http(core, poller, clock, stop)
    assert poller.started == [start + 15.0]
    first_renewal = start + L / 3
    # Each renewal period counts from the sending of the previous renewal, not its answer.
    second_renewal = first_renewal + L / 3
    third = second_renewal + L / 3
    # The hanging renewal was sent at ``third``; the deadline of the lease before it counts
    # from the sending of the renewal that got it (second_renewal), not from its answer.
    assert poller.stopped == [second_renewal + L - MARGIN + STOP_TAKES]
    assert third < poller.stopped[0]
    assert polling_ended_before_every_lease_expired(core, poller)
    assert core.paths().count("/internal/v1/leases/bot/release") == 0


async def test_a_hanging_core_renewal_is_cancelled_at_the_deadline(clock):
    stop = asyncio.Event()
    core, poller = SlowCore(clock, takes=[0.0, "hang"], stop=stop), Poller(clock)
    start = clock.now
    await run_over_http(core, poller, clock, stop)
    assert poller.stopped == [start + L - MARGIN + STOP_TAKES]
    assert core.cancelled == ["/internal/v1/leases/bot/renew"]
    assert polling_ended_before_every_lease_expired(core, poller)


async def test_a_lease_lost_in_the_core_stops_polling_at_once(clock):
    from fakes.core_gateway import envelope

    stop = asyncio.Event()
    core, poller = SlowCore(clock, stop=stop), Poller(clock)
    core.answer("/internal/v1/leases/bot/renew", envelope(409, "LEASE_LOST"))
    start = clock.now
    await run_over_http(core, poller, clock, stop)
    assert poller.stopped == [start + L / 3 + STOP_TAKES]


async def test_an_acquire_answered_too_late_by_the_core_is_released_at_once(clock):
    stop = asyncio.Event()
    core, poller = SlowCore(clock, takes=[L - MARGIN + 1.0], stop=stop), Poller(clock)
    start = clock.now
    await run_over_http(core, poller, clock, stop)
    assert core.bodies("/internal/v1/leases/bot/release")[0] == {"holder": "bot-1", "generation": "1"}
    # Polling started only under the next lease, taken after the standby period.
    assert poller.started == [start + L - MARGIN + 1.0 + STANDBY]
    assert polling_ended_before_every_lease_expired(core, poller)


async def test_a_stop_over_http_releases_the_lease_after_polling_stopped(clock):
    stop = asyncio.Event()
    core, poller = SlowCore(clock, stop=stop), Poller(clock)
    gateway = build_gateway(core_settings(), transport=core.transport())
    leader = Leader(gateway.leases, poller, config=CONFIG, monotonic=clock.monotonic, sleep=clock.sleep)
    task = asyncio.create_task(leader.run(stop))
    for _ in range(50):
        await asyncio.sleep(0)
        if leader.leading:
            break
    assert leader.leading is True
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    await gateway.aclose()
    assert core.paths() == ["/internal/v1/leases/bot/acquire", "/internal/v1/leases/bot/release"]
    # Released after the final getUpdates: the core ended the lease at that moment.
    assert core.lease.expires_at == clock.now


async def test_a_failing_release_is_logged_by_class_and_the_leader_goes_on(clock, caplog):
    import logging

    import httpx

    stop = asyncio.Event()
    core, poller = SlowCore(clock, takes=[L - MARGIN + 1.0], stop=stop, acquisitions=2), Poller(clock)
    # The first lease answers after its deadline and is given back at once; that release
    # fails in transport. The leader stands by and takes the next lease.
    core.answer("/internal/v1/leases/bot/release", httpx.ConnectError("refused"))
    caplog.set_level(logging.WARNING, logger="wortly_telegram.leader")
    await run_over_http(core, poller, clock, stop)
    assert "bot lease release failed: CoreUnavailable" in caplog.text
    assert "refused" not in caplog.text
    assert len(poller.started) == 1
