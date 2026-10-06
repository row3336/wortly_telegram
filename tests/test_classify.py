"""Every row of design T-D §3.3, produced by PTB's own request code over a fake transport."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from wortly.jobs.channel import ChannelResult
from wortly_telegram.delivery.classify import classify, never_left


async def failure(telegram, script) -> BaseException:
    """Run one sendMessage against the scripted fake and return what PTB raised."""
    script(telegram)
    async with telegram.bot() as bot:
        with pytest.raises(Exception) as raised:
            await bot.send_message(chat_id=4242, text="hello")
    assert len(telegram.calls_to("sendMessage")) == 1
    return raised.value


def transport_error(kind: type[httpx.TransportError]):
    def script(telegram):
        telegram.raise_error("sendMessage", kind("simulated", request=None))
    return script


ROWS = [
    # (row id, script, outcome, reason)
    ("429-with-retry-after", lambda t: t.fail("sendMessage", 429, "Too Many Requests", retry_after=17),
     "known_failed", "rate_limited"),
    ("429-without-retry-after", lambda t: t.fail("sendMessage", 429, "Too Many Requests"), "unknown", None),
    ("connect-error", transport_error(httpx.ConnectError), "known_failed", "not_sent"),
    ("connect-timeout", transport_error(httpx.ConnectTimeout), "known_failed", "not_sent"),
    ("pool-timeout", transport_error(httpx.PoolTimeout), "known_failed", "not_sent"),
    ("401", lambda t: t.fail("sendMessage", 401, "Unauthorized"), "known_failed", "channel_config"),
    ("404", lambda t: t.fail("sendMessage", 404, "Not Found"), "known_failed", "channel_config"),
    ("403", lambda t: t.fail("sendMessage", 403, "Forbidden: bot was blocked by the user"),
     "known_failed", "blocked"),
    ("400", lambda t: t.fail("sendMessage", 400, "Bad Request: chat not found"), "known_failed", "rejected"),
    ("400-chat-migrated", lambda t: t.fail("sendMessage", 400, "migrated", migrate_to_chat_id=-100123),
     "known_failed", "rejected"),
    ("read-timeout", transport_error(httpx.ReadTimeout), "unknown", None),
    ("write-timeout", transport_error(httpx.WriteTimeout), "unknown", None),
    ("read-error", transport_error(httpx.ReadError), "unknown", None),
    ("remote-protocol-error", transport_error(httpx.RemoteProtocolError), "unknown", None),
    ("500", lambda t: t.fail("sendMessage", 500, "Internal Server Error"), "unknown", None),
    ("502", lambda t: t.fail("sendMessage", 502, "Bad Gateway"), "unknown", None),
    ("409-conflict", lambda t: t.fail("sendMessage", 409, "Conflict"), "unknown", None),
    ("2xx-unparsable", lambda t: t.raw("sendMessage", 200, b"<html>proxy</html>"), "unknown", None),
    ("2xx-wrong-shape", lambda t: t.answer("sendMessage", {"unexpected": True}), "unknown", None),
]


@pytest.mark.parametrize(("script", "outcome", "reason"), [row[1:] for row in ROWS], ids=[row[0] for row in ROWS])
async def test_each_answer_maps_to_its_class(telegram, script, outcome, reason):
    result = classify(await failure(telegram, script))
    assert type(result) is ChannelResult
    assert (result.outcome, result.reason) == (outcome, reason)


async def test_a_rate_limit_carries_telegrams_retry_after(telegram):
    result = classify(await failure(telegram, lambda t: t.fail("sendMessage", 429, "Too Many", retry_after=17)))
    assert result.retry_after == timedelta(seconds=17)


def test_bad_request_and_timeouts_are_network_errors_so_order_matters():
    # The reason for the order of the checks (finding 9): a classifier that asked for
    # NetworkError first would call a 400 unknown.
    assert issubclass(BadRequest, NetworkError) and issubclass(TimedOut, NetworkError)
    assert classify(BadRequest("Bad Request: message text is empty")).reason == "rejected"


def test_only_a_transport_cause_proves_the_request_never_left():
    plain_timeout = TimedOut()
    assert not never_left(plain_timeout)
    assert classify(plain_timeout).outcome == "unknown"
    wrapped = NetworkError("Unknown error in HTTP implementation")
    wrapped.__cause__ = RuntimeError("anything")
    assert classify(wrapped).outcome == "unknown"
    assert classify(ValueError("not from PTB")).outcome == "unknown"
