"""The polling cycle over the fake getUpdates (design T-D §9.1): the leader's poller is PTB's Updater."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json

from fakes.telegram_api import message_update, user
from wortly_telegram.bot_main import UpdaterPoller
from wortly_telegram.leader import Leader, LeaderConfig
from wortly_telegram.router import ALLOWED_UPDATES


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
    assert json.loads(polls[0].params["allowed_updates"]) == list(ALLOWED_UPDATES)
    assert polls[0].params["timeout"] == "7"
    # The last getUpdates, made while stopping, confirms what was fetched (offset = last id + 1).
    assert polls[-1].params["offset"] == "4101"
    assert store.released == ["lease"]
    calls_after_stop = len(telegram.calls)
    await asyncio.sleep(0.05)
    assert len(telegram.calls) == calls_after_stop
