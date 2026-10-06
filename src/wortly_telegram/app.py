"""PTB objects of the adapter: the Bot API client and the bot's Application (design T-D §7.1).

``make_bot`` builds the telegram.Bot of either role from its explicit settings: two
HTTPXRequest objects (PTB keeps getUpdates on its own connection pool) with the
configured timeouts. It is a plain Bot: no AIORateLimiter, which would wait inside a
call (design §3.5 п.3), and no callback-data cache. Tests pass an httpx transport
(design §9.1).

``build_application`` wires the bot role's handlers (router.install). The Mini App
address is a required argument with no default (Q15); updates are processed one at a
time, and no job queue runs in the bot: scheduled work belongs to the core's worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import logging
from typing import Any

from telegram import Bot
from telegram.ext import Application, ApplicationBuilder, ContextTypes
from telegram.request import HTTPXRequest

from .config import TelegramSettings
from .router import BotContext, Gateway, ServiceFunction, discover, install
from .webapp import checked_address


logger = logging.getLogger(__name__)


def make_bot(settings: TelegramSettings, *, httpx_kwargs: dict[str, Any] | None = None) -> Bot:
    def request() -> HTTPXRequest:
        return HTTPXRequest(connect_timeout=settings.connect_timeout.total_seconds(),
                            read_timeout=settings.read_timeout.total_seconds(),
                            write_timeout=settings.write_timeout.total_seconds(),
                            pool_timeout=settings.pool_timeout.total_seconds(),
                            httpx_kwargs=httpx_kwargs)

    return Bot(settings.bot_token, request=request(), get_updates_request=request())


async def _log_failure(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # The class only: an exception's text may quote Telegram's answer or user data (TC:273).
    logger.warning("update not handled: %s", type(context.error).__name__)


def build_application(bot: Bot, *, gateway: Gateway, web_app_url: str,
                      functions: Sequence[ServiceFunction] | None = None,
                      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> Application:
    """The bot role's Application; ``functions`` defaults to the discovered service functions."""
    application = ApplicationBuilder().bot(bot).job_queue(None).build()
    context = BotContext(bot=application.bot, gateway=gateway, web_app_url=checked_address(web_app_url),
                         sleep=sleep)
    install(application, context, discover() if functions is None else functions)
    application.add_error_handler(_log_failure)
    return application
