"""TelegramChannel.deliver: one sendMessage, a ChannelResult with its class, no wait after sending.

The channel runs as in the delivery process: deliver() is called from a plain thread
and hands the send to the event loop that owns the Bot (delivery.main.loop_runner).
"""

from __future__ import annotations

from datetime import timedelta
import time

import httpx
import pytest
from telegram import Bot
from telegram.ext import ExtBot

from conftest import TELEGRAM_SETTINGS
from fakes.jobs import envelope, snapshot
from fakes.telegram_api import WEB_APP_URL
from wortly.jobs.channel import SENT, ChannelResult
from wortly_telegram.app import make_bot
from wortly_telegram.delivery.channel import TelegramChannel
from wortly_telegram.delivery.main import loop_runner
from wortly_telegram.messages.registry import discover


class RecordingCapacity:
    """Records pauses; the channel must never wait on the gate itself."""

    def __init__(self) -> None:
        self.pauses: list[timedelta] = []
        self.waits: list[str] = []

    def pause(self, duration: timedelta) -> None:
        self.pauses.append(duration)

    def wait_until_open(self) -> None:
        self.waits.append("wait_until_open")

    def wait_for_slot(self) -> None:
        self.waits.append("wait_for_slot")


@pytest.fixture
def capacity() -> RecordingCapacity:
    return RecordingCapacity()


@pytest.fixture
def make_channel(telegram, bot_loop, capacity):
    run = loop_runner(bot_loop)
    bots = []

    def make(web_app_url: str = WEB_APP_URL) -> TelegramChannel:
        bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
        run(bot.initialize())
        bots.append(bot)
        return TelegramChannel(bot, templates=discover(), web_app_url=web_app_url, capacity=capacity, run=run)

    yield make
    for bot in bots:
        run(bot.shutdown())


def test_a_sent_message_is_one_send_message_with_the_app_button(telegram, make_channel, capacity):
    result = make_channel().deliver(snapshot(facts=envelope(language="uk", telegram_user_id="777000")))
    assert result is SENT
    [call] = telegram.calls_to("sendMessage")
    assert call.params["chat_id"] == "777000"
    assert call.params["text"] == "✅ Доступ підтверджено! Натисніть «Старт»."
    assert "parse_mode" not in call.params
    [[button]] = call.json("reply_markup")["inline_keyboard"]
    assert button == {"text": "Старт", "web_app": {"url": WEB_APP_URL}}
    assert capacity.waits == [] and capacity.pauses == []


def transport(kind):
    return lambda t: t.raise_error("sendMessage", kind("simulated", request=None))


SCRIPTS = [
    ("429", lambda t: t.fail("sendMessage", 429, "Too Many Requests", retry_after=30), "known_failed", "rate_limited"),
    ("429-bare", lambda t: t.fail("sendMessage", 429, "Too Many Requests"), "unknown", None),
    ("read-timeout", transport(httpx.ReadTimeout), "unknown", None),
    ("connect-error", transport(httpx.ConnectError), "known_failed", "not_sent"),
    ("403", lambda t: t.fail("sendMessage", 403, "Forbidden"), "known_failed", "blocked"),
    ("400", lambda t: t.fail("sendMessage", 400, "Bad Request"), "known_failed", "rejected"),
    ("401", lambda t: t.fail("sendMessage", 401, "Unauthorized"), "known_failed", "channel_config"),
    ("500", lambda t: t.fail("sendMessage", 500, "Internal"), "unknown", None),
]


@pytest.mark.parametrize(("script", "outcome", "reason"), [row[1:] for row in SCRIPTS], ids=[row[0] for row in SCRIPTS])
def test_every_failure_is_exactly_one_request_and_a_classified_result(telegram, make_channel, capacity,
                                                                      script, outcome, reason):
    channel = make_channel()
    script(telegram)
    result = channel.deliver(snapshot())
    # One external call per committed attempt: no retry in the channel or in PTB (TC:282).
    assert len(telegram.calls_to("sendMessage")) == 1
    assert type(result) is ChannelResult
    assert (result.outcome, result.reason) == (outcome, reason)
    assert capacity.waits == []


def test_a_rate_limit_pauses_the_gate_and_does_not_wait_inside_deliver(telegram, make_channel, capacity):
    channel = make_channel()
    telegram.fail("sendMessage", 429, "Too Many Requests", retry_after=30)
    started = time.monotonic()
    result = channel.deliver(snapshot())
    assert time.monotonic() - started < 5
    assert result.retry_after == timedelta(seconds=30)
    # The pause is the next claim's to wait for, not this attempt's (§3.5 п.4).
    assert capacity.pauses == [timedelta(seconds=30)]
    assert capacity.waits == []


def test_a_rejected_token_marks_the_channel(telegram, make_channel):
    channel = make_channel()
    assert channel.token_rejected is False
    telegram.fail("sendMessage", 401, "Unauthorized")
    channel.deliver(snapshot())
    assert channel.token_rejected is True


@pytest.mark.parametrize("facts", [
    envelope(language="de"),
    envelope(telegram_user_id="not-an-id"),
    envelope(body={"text": "x"}),
    {"schema": 1},
], ids=["unknown-language", "bad-recipient", "unexpected-body", "bare-envelope"])
def test_a_fault_before_the_network_is_pre_send_error_with_no_request(telegram, make_channel, facts):
    result = make_channel().deliver(snapshot(facts=facts))
    assert (result.outcome, result.reason) == ("known_failed", "pre_send_error")
    assert telegram.calls_to("sendMessage") == []


def test_an_unknown_kind_is_pre_send_error_with_no_request(telegram, make_channel):
    result = make_channel().deliver(snapshot(kind="weekly_report", facts=envelope(kind="weekly_report")))
    assert result.reason == "pre_send_error"
    assert telegram.calls_to("sendMessage") == []


def test_a_bad_mini_app_address_is_pre_send_error_with_no_request(telegram, make_channel):
    result = make_channel(web_app_url="http://app.example.test/").deliver(snapshot())
    assert result.reason == "pre_send_error"
    assert telegram.calls_to("sendMessage") == []


def test_the_delivery_bot_has_no_rate_limiter():
    # AIORateLimiter waits inside the call, after the sending decision (TC:282, §3.5 п.3).
    bot = make_bot(TELEGRAM_SETTINGS)
    assert type(bot) is Bot and not isinstance(bot, ExtBot)
