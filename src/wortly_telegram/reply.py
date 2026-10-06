"""Replies of the bot in a chat: temporary rule ВП-28 until the owner answers Q32 (fork Р4).

A reply answers a user's own action; it is not a delivery job, so TC:282 does not
govern it (design T-D §3.5 п.6). Until Q32 is answered the bot repeats as the old bot
did (OLD bot/common/navigation.py:108-151; "may have arrived → stay silent" is the
owner's decision of 2026-07-11):

- RetryAfter: wait retry_after + 1 s and send again;
- a request that never left (httpx connect error, connect timeout, pool timeout):
  wait 2, 4, then 5 s and send again;
- at most three repeats in all; after them the error goes up to the handler;
- a 400 (BadRequest, e.g. an edit of a deleted message) is Telegram's refusal: no
  repeat and no notice, a log line that says it was refused (T2-QA З3);
- any other network error (a timeout after sending, a broken connection, an error
  answer such as 5xx) may follow a delivered message: no repeat and no notice,
  a log line with the exception class only.

Other errors (Forbidden, InvalidToken, ChatMigrated, Conflict) go up at once, as in
the old bot. This module is the only place that changes after Q32.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import TypeVar

from telegram.error import BadRequest, NetworkError, RetryAfter

from .delivery.classify import never_left, retry_after


logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_REPEATS = 3
_LONGEST_NETWORK_PAUSE = 5


async def send_reply(call: Callable[[], Awaitable[T]], *,
                     sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> T | None:
    """Make the reply call; None when it was refused or may have arrived, and is not repeated."""
    repeats = 0
    while True:
        try:
            return await call()
        except RetryAfter as error:
            repeats += 1
            if repeats > MAX_REPEATS:
                raise
            await sleep(int(retry_after(error).total_seconds()) + 1)
        except BadRequest as error:
            # A subclass of NetworkError, but Telegram answered: the call was refused, not lost.
            logger.warning("reply refused by Telegram; not repeated: %s", type(error).__name__)
            return None
        except NetworkError as error:
            if not never_left(error):
                logger.warning("reply may have reached Telegram; not repeated: %s", type(error).__name__)
                return None
            repeats += 1
            if repeats > MAX_REPEATS:
                raise
            await sleep(min(_LONGEST_NETWORK_PAUSE, repeats * 2))
