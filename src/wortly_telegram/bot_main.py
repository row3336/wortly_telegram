"""Process entry of the bot role: ``python -m wortly_telegram.bot_main --config <file>``.

Design T-D §2.1, §2.4.1, §2.4.4. The Application processes updates; only the holder
of the "bot" lease polls (leader.py), and only for the update types the router
handles (no edited messages, ВП-6). getMe must accept the token at start, otherwise
the process does not start. On SIGTERM or SIGINT polling stops (PTB confirms the
fetched updates), the lease is released, queued updates are finished, and the process
ends (§2.4.4).

Variant A of fork Р1 (until Q28): the bot reaches the core through gateway.py on its
own writable-leader connections, each with a lock_timeout so a stuck lock holder
cannot stall the bot forever. Everything runs inside private_logs (logs.py). The core
is imported inside the functions that start the process.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from datetime import timedelta
import logging
from pathlib import Path
import signal
import sys

from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application

from .router import ALLOWED_UPDATES


logger = logging.getLogger(__name__)


class UpdaterPoller:
    """The leader's poller: PTB's Updater with this bot's update types and long-poll timeout."""

    def __init__(self, application: Application, *, poll_timeout: timedelta) -> None:
        self._application = application
        self._poll_timeout = poll_timeout

    @staticmethod
    def _polling_failed(error: TelegramError) -> None:
        # Conflict here means another getUpdates consumer (an old bot on the same token).
        logger.warning("polling error: %s", type(error).__name__)

    async def start(self) -> None:
        await self._application.updater.start_polling(timeout=self._poll_timeout,
                                                      allowed_updates=list(ALLOWED_UPDATES),
                                                      error_callback=self._polling_failed)

    async def stop(self) -> None:
        if self._application.updater.running:
            await self._application.updater.stop()


async def _serve(settings, *, gateway, leases) -> int:
    from .app import build_application, make_bot
    from .leader import Leader

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, stop.set)
    application = build_application(make_bot(settings.telegram), gateway=gateway,
                                    web_app_url=settings.telegram.web_app_url)
    try:
        await application.initialize()
    except InvalidToken:
        logger.error("bot not started: Telegram rejected the bot token")
        return 2
    except TelegramError as error:
        logger.error("bot not started: getMe failed: %s", type(error).__name__)
        return 1
    try:
        await application.start()
        leader = Leader(leases, UpdaterPoller(application, poll_timeout=settings.poll_timeout),
                        config=settings.leader)
        await leader.run(stop)
    finally:
        if application.running:
            await application.stop()
        await application.shutdown()
    return 0


def serve(settings) -> int:
    from wortly.jobs.leases import LeaseStore
    from wortly.jobs.worker import lock_timeout_connections, utc_now
    from wortly.operations.connections import open_connection_pool

    from .gateway import build_gateway

    pool = open_connection_pool(settings.database, name="wortly-bot")
    try:
        connections = lock_timeout_connections(pool.connection, settings.lock_timeout)
        gateway = build_gateway(connections, clock=utc_now, session_lifetime=settings.session_lifetime)
        return asyncio.run(_serve(settings, gateway=gateway, leases=LeaseStore(connections)))
    finally:
        pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    from wortly.operations.config import ConfigurationError

    from .config import load_bot_settings
    from .logs import log_secrets, private_logs
    from .router import RouterError

    parser = argparse.ArgumentParser(prog="python -m wortly_telegram.bot_main",
                                     description="Run the Wortly Telegram bot from an explicit configuration file.")
    parser.add_argument("--config", required=True, type=Path, help="TOML file; every key is required")
    arguments = parser.parse_args(argv)
    try:
        settings = load_bot_settings(arguments.config)
    except ConfigurationError as error:
        print(f"wortly bot: configuration error: {error}", file=sys.stderr)
        return 2
    with private_logs(log_secrets(settings.telegram.bot_token, settings.database)):
        try:
            return serve(settings)
        except RouterError as error:
            # The router's own message: command and module names, never data.
            logger.error("bot not started: %s", error)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
