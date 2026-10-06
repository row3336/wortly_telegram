"""Every row of design T-D §3.3, produced by PTB's own request code over a fake transport."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from telegram.error import (
    BadRequest, ChatMigrated, Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut,
)

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
    # Only the confirmed block closes a job (answer 2.5); every other 403 stays repeatable.
    ("403-not-started", lambda t: t.fail("sendMessage", 403, "Forbidden: bot can't initiate conversation with a user"),
     "known_failed", "forbidden"),
    ("403-deactivated", lambda t: t.fail("sendMessage", 403, "Forbidden: user is deactivated"),
     "known_failed", "forbidden"),
    ("403-bare", lambda t: t.fail("sendMessage", 403, "Forbidden"), "known_failed", "forbidden"),
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


def caused(error: BaseException, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


def never_left_cause() -> httpx.ConnectError:
    return httpx.ConnectError("simulated", request=None)


# The order of design §3.3 as a decision table: what Telegram answered explicitly wins over
# the transport cause, "never left" comes next, everything else is unknown.
ORDER = [
    ("retry-after-over-cause", lambda: caused(RetryAfter(5), never_left_cause()), ("known_failed", "rate_limited")),
    ("blocked-over-cause", lambda: caused(Forbidden("Forbidden: bot was blocked by the user"), never_left_cause()),
     ("known_failed", "blocked")),
    ("forbidden-over-cause", lambda: caused(Forbidden("f"), never_left_cause()), ("known_failed", "forbidden")),
    ("migrated-over-cause", lambda: caused(ChatMigrated(-100), never_left_cause()), ("known_failed", "rejected")),
    ("bad-request-over-cause", lambda: caused(BadRequest("b"), never_left_cause()), ("known_failed", "rejected")),
    ("invalid-token-over-cause", lambda: caused(InvalidToken("i"), never_left_cause()),
     ("known_failed", "channel_config")),
    ("timed-out-never-left", lambda: caused(TimedOut(), httpx.ConnectTimeout("x", request=None)),
     ("known_failed", "not_sent")),
    ("timed-out-pool", lambda: caused(TimedOut(), httpx.PoolTimeout("x", request=None)),
     ("known_failed", "not_sent")),
    ("network-error-never-left", lambda: caused(NetworkError("n"), never_left_cause()), ("known_failed", "not_sent")),
    ("timed-out-read", lambda: caused(TimedOut(), httpx.ReadTimeout("x", request=None)), ("unknown", None)),
    ("timed-out-write", lambda: caused(TimedOut(), httpx.WriteTimeout("x", request=None)), ("unknown", None)),
    ("network-error-proxy", lambda: caused(NetworkError("n"), httpx.ProxyError("x", request=None)),
     ("unknown", None)),
    ("conflict-over-cause", lambda: caused(Conflict("c"), never_left_cause()), ("unknown", None)),
    ("no-cause", lambda: NetworkError("Too Many Requests"), ("unknown", None)),
    ("foreign-error-with-cause", lambda: caused(RuntimeError("bridge"), never_left_cause()), ("unknown", None)),
]


@pytest.mark.parametrize(("make", "expected"), [row[1:] for row in ORDER], ids=[row[0] for row in ORDER])
def test_the_order_of_the_checks(make, expected):
    result = classify(make())
    assert (result.outcome, result.reason) == expected


@pytest.mark.parametrize("message", [
    "Forbidden: bot was blocked by the user",
    "forbidden: BOT WAS BLOCKED BY THE USER",
    "bot was blocked by the user",
])
def test_the_blocked_marker_is_the_confirmed_block_whatever_the_case(message):
    assert classify(Forbidden(message)).reason == "blocked"


@pytest.mark.parametrize("message", [
    "Forbidden: bot was kicked from the group chat",
    "Forbidden: the bot was blocked",
    "",
])
def test_an_unknown_403_text_is_not_a_confirmed_block(message):
    assert classify(Forbidden(message)).reason == "forbidden"
