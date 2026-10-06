"""Bot replies under ВП-28 (as the old bot): repeat only what certainly was not delivered."""

from __future__ import annotations

import logging

import httpx
import pytest
from telegram import Message
from telegram.error import Forbidden, NetworkError, RetryAfter

from wortly_telegram.reply import MAX_REPEATS, send_reply


def never_sent(telegram, times: int) -> None:
    for _ in range(times):
        telegram.raise_error("sendMessage", httpx.ConnectError("refused", request=None))


async def reply(telegram, sleeps):
    async with telegram.bot() as bot:
        return await send_reply(lambda: bot.send_message(chat_id=5005, text="hi"), sleep=sleeps)


async def test_a_rate_limit_is_repeated_after_retry_after_plus_one_second(telegram, sleeps):
    telegram.fail("sendMessage", 429, "Too Many Requests", retry_after=7)
    telegram.fail("sendMessage", 429, "Too Many Requests", retry_after=3)
    assert isinstance(await reply(telegram, sleeps), Message)
    assert len(telegram.calls_to("sendMessage")) == 3
    assert sleeps.pauses == [8, 4]


async def test_rate_limits_are_repeated_three_times_at_most(telegram, sleeps):
    for _ in range(MAX_REPEATS + 1):
        telegram.fail("sendMessage", 429, "Too Many Requests", retry_after=1)
    with pytest.raises(RetryAfter):
        await reply(telegram, sleeps)
    assert len(telegram.calls_to("sendMessage")) == 1 + MAX_REPEATS


async def test_a_request_that_never_left_is_repeated_with_growing_pauses(telegram, sleeps):
    never_sent(telegram, 2)
    assert isinstance(await reply(telegram, sleeps), Message)
    assert sleeps.pauses == [2, 4]


async def test_requests_that_never_left_are_repeated_three_times_at_most(telegram, sleeps):
    never_sent(telegram, MAX_REPEATS + 1)
    with pytest.raises(NetworkError):
        await reply(telegram, sleeps)
    assert len(telegram.calls_to("sendMessage")) == 1 + MAX_REPEATS
    assert sleeps.pauses == [2, 4, 5]


@pytest.mark.parametrize("script", [
    lambda t: t.raise_error("sendMessage", httpx.ReadTimeout("slow", request=None)),
    lambda t: t.raise_error("sendMessage", httpx.RemoteProtocolError("cut", request=None)),
    lambda t: t.fail("sendMessage", 500, "Internal Server Error"),
    lambda t: t.fail("sendMessage", 429, "Too Many Requests"),
    lambda t: t.fail("sendMessage", 400, "Bad Request"),
], ids=["read-timeout", "broken-connection", "500", "429-bare", "400"])
async def test_a_reply_that_may_have_arrived_is_not_repeated_and_only_logged(telegram, sleeps, caplog, script):
    script(telegram)
    with caplog.at_level(logging.DEBUG, logger="wortly_telegram"):
        assert await reply(telegram, sleeps) is None
    assert len(telegram.calls_to("sendMessage")) == 1
    assert sleeps.pauses == []
    [record] = [record for record in caplog.records if record.name == "wortly_telegram.reply"]
    assert record.levelno == logging.WARNING and "hi" not in record.getMessage()


async def test_other_errors_go_up_at_once(telegram, sleeps):
    telegram.fail("sendMessage", 403, "Forbidden: bot was blocked by the user")
    with pytest.raises(Forbidden):
        await reply(telegram, sleeps)
    assert len(telegram.calls_to("sendMessage")) == 1
