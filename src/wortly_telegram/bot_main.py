"""Process entry of the bot role: ``python -m wortly_telegram.bot_main --config <file>``.

Design T-D §2.1, §2.4.1, §2.4.4. The Application processes updates; only the holder
of the "bot" lease polls (leader.py), and only for the update types the router
handles (no edited messages, ВП-6). getMe must accept the token at start, otherwise
the process does not start. On SIGTERM or SIGINT polling stops (PTB confirms the
fetched updates), the lease is released, queued updates are finished, and the process
ends (§2.4.4).

Variant Б of fork Р1 (answer 2.4): the bot opens no database and imports no database
driver. It reaches the core, the admission operations and the poller's lease alike,
only through the core's internal API (gateway.py, one client, closed on exit).
Everything runs inside private_logs (logs.py), which also hides the service secret.
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


async def _run(settings) -> int:
    from .gateway import build_gateway

    gateway = build_gateway(settings.core)
    try:
        return await _serve(settings, gateway=gateway, leases=gateway.leases)
    finally:
        await gateway.aclose()


def serve(settings) -> int:
    return asyncio.run(_run(settings))


def main(argv: Sequence[str] | None = None) -> int:
    from wortly.operations.log_privacy import token_forms

    from .config import BotConfigurationError, load_bot_settings
    from .logs import private_logs
    from .router import RouterError

    parser = argparse.ArgumentParser(prog="python -m wortly_telegram.bot_main",
                                     description="Run the Wortly Telegram bot from an explicit configuration file.")
    parser.add_argument("--config", required=True, type=Path, help="TOML file; every key is required")
    arguments = parser.parse_args(argv)
    try:
        settings = load_bot_settings(arguments.config)
    except BotConfigurationError as error:
        print(f"wortly bot: configuration error: {error}", file=sys.stderr)
        return 2
    # The bot holds two secrets: the token in every written form and the service secret.
    with private_logs((*token_forms(settings.telegram.bot_token), settings.core.service_secret)):
        try:
            return serve(settings)
        except RouterError as error:
            # The router's own message: command and module names, never data.
            logger.error("bot not started: %s", error)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
