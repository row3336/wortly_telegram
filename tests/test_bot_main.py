"""The polling cycle over the fake getUpdates (design T-D §9.1): the leader's poller is PTB's Updater."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import signal
from types import SimpleNamespace

import httpx
import pytest

from conftest import TELEGRAM_SETTINGS
from fakes.telegram_api import message_update, user
from wortly_telegram.bot_main import UpdaterPoller, _serve
from wortly_telegram.leader import Leader, LeaderConfig


class Store:
    def __init__(self) -> None:
        self.released = []

    def acquire(self, role, *, holder, duration):
        return "lease"

    def renew(self, lease, *, duration):
        return lease

    def release(self, lease):
        self.released.append(lease)
        return True


async def test_the_leader_polls_answers_and_confirms_the_updates_when_it_stops(telegram, make_application):
    application = await make_application()
    await application.start()
    telegram.updates(message_update("/start", from_user=user(5005), update_id=4100))
    stop = asyncio.Event()
    store = Store()
    config = LeaderConfig(holder="bot-1", lease=timedelta(seconds=30), stop_margin=timedelta(seconds=5),
                          standby_retry=timedelta(seconds=1))
    leader = Leader(store, UpdaterPoller(application, poll_timeout=timedelta(seconds=7)), config=config)
    running = asyncio.create_task(leader.run(stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if telegram.calls_to("sendMessage"):
            break
    stop.set()
    await asyncio.wait_for(running, timeout=10)
    await application.stop()

    assert len(telegram.calls_to("sendMessage")) == 1
    polls = telegram.calls_to("getUpdates")
    # Spelled out, not read from the constant: a new update type must be a deliberate change (ВП-6).
    assert json.loads(polls[0].params["allowed_updates"]) == ["message", "callback_query"]
    assert polls[0].params["timeout"] == "7"
    # The last getUpdates, made while stopping, confirms what was fetched (offset = last id + 1).
    assert polls[-1].params["offset"] == "4101"
    assert store.released == ["lease"]
    calls_after_stop = len(telegram.calls)
    await asyncio.sleep(0.05)
    assert len(telegram.calls) == calls_after_stop


def spy_on_signal_handlers(monkeypatch) -> dict:
    """Record what the process registers for signals instead of registering it with the OS."""
    handlers = {}
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler",
                        lambda number, callback, *args: handlers.setdefault(number, callback))
    return handlers


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
async def test_the_bot_process_stops_on_a_signal_and_releases_the_lease(monkeypatch, telegram, gateway, number):
    from wortly_telegram import app

    real_make_bot = app.make_bot

    async def handle(request):
        if request.url.path.endswith("/getUpdates"):
            await asyncio.sleep(0.02)  # a long poll that is not instant, as the real one
        return telegram.handle(request)

    monkeypatch.setattr(app, "make_bot", lambda settings: real_make_bot(
        settings, httpx_kwargs={"transport": httpx.MockTransport(handle)}))
    handlers = spy_on_signal_handlers(monkeypatch)
    store = Store()
    settings = SimpleNamespace(telegram=TELEGRAM_SETTINGS, poll_timeout=timedelta(seconds=1),
                               leader=LeaderConfig(holder="bot-1", lease=timedelta(seconds=30),
                                                   stop_margin=timedelta(seconds=5),
                                                   standby_retry=timedelta(seconds=1)))
    task = asyncio.create_task(_serve(settings, gateway=gateway, leases=store))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if telegram.calls_to("getUpdates"):
            break
    assert telegram.calls_to("getUpdates"), "polling started"
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[number]()
    assert await asyncio.wait_for(task, timeout=10) == 0
    assert store.released == ["lease"]
    calls = len(telegram.calls)
    await asyncio.sleep(0.1)
    assert len(telegram.calls) == calls, "nothing is sent or polled after the process ended"
