"""Shared fixtures: a fake Bot API behind PTB's real client and a fake core gateway.

No test opens a socket or a port; the database is used only by the integration test,
and only when psycopg is installed (design T-D §9.5).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
import threading

import pytest

from fakes import trail as trail_fakes
from fakes.core_gateway import FakeCoreGateway
from fakes.telegram_api import TOKEN, WEB_APP_URL, FakeTelegram
from wortly_telegram.config import TelegramSettings


TELEGRAM_SETTINGS = TelegramSettings(bot_token=TOKEN, web_app_url=WEB_APP_URL,
                                     connect_timeout=timedelta(seconds=1), read_timeout=timedelta(seconds=1),
                                     write_timeout=timedelta(seconds=1), pool_timeout=timedelta(seconds=1))


@pytest.fixture
def trail_lines() -> Iterator[trail_fakes.TrailLines]:
    """The lines of the user-path trail written during the test (R3-03)."""
    handler = trail_fakes.attach()
    yield handler
    trail_fakes.detach(handler)


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def gateway() -> FakeCoreGateway:
    return FakeCoreGateway()


class Sleeps:
    """Records the pauses of reply repeats instead of sleeping."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def make_application(telegram, gateway, sleeps):
    """Build and initialise the bot's Application over the fake Bot API; updates go to process_update."""
    from wortly_telegram.app import build_application

    built = []

    async def make(*, web_app_url: str = WEB_APP_URL, functions=None):
        application = build_application(telegram.bot(), gateway=gateway, web_app_url=web_app_url,
                                         functions=functions, sleep=sleeps)
        await application.initialize()
        built.append(application)
        return application

    yield make
    for application in built:
        await application.shutdown()


async def feed(application, data: dict) -> None:
    """Hand one update to the Application exactly as the poller would (design §9.1)."""
    from telegram import Update

    await application.process_update(Update.de_json(data, application.bot))


@pytest.fixture
def bot_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """The delivery process's main loop, owning the Bot, in a thread of its own."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="test-bot-loop", daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()
