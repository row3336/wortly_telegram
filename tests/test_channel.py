"""TelegramChannel.deliver: one sendMessage, a ChannelResult with its class, no wait after sending.

The channel runs as in the delivery process: deliver() is called from a plain thread
and hands the send to the event loop that owns the Bot (delivery.main.loop_runner).
"""

from __future__ import annotations

from datetime import timedelta
import logging
import time

import httpx
import pytest
from telegram import Bot
from telegram.ext import ExtBot

from conftest import TELEGRAM_SETTINGS
from fakes.jobs import envelope, snapshot
from fakes.telegram_api import WEB_APP_URL
from wortly.jobs.channel import SENT, ChannelResult
from wortly_telegram import logs
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
    ("403-blocked", lambda t: t.fail("sendMessage", 403, "Forbidden: bot was blocked by the user"),
     "known_failed", "blocked"),
    ("403", lambda t: t.fail("sendMessage", 403, "Forbidden"), "known_failed", "forbidden"),
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


# --- inbox_message: the sender block and number reach the owner as plain text; a complaint has its card button ---

INBOX_ID = "5d1a7c3e-0000-4000-8000-000000000001"
CARD = {"card_id": "5d1a7c3e-0000-4000-8000-000000000002", "lemma": "Haus"}
SENDER = {"first_name": "Anna", "username": "anna_k"}
SENDER_LABELS = {"ru": "От:", "uk": "Від:", "en": "From:"}
NUMBER_LABELS = {"ru": "Номер обращения:", "uk": "Номер звернення:", "en": "Message number:"}
CARD_LABELS = {"ru": "Открыть карточку", "uk": "Відкрити картку", "en": "Open the card"}
INBOX_CASES = [("complaint", CARD), ("feedback", None)]


def inbox_facts(language, category, card, **extra):
    body = {"inbox_id": INBOX_ID, "category": category, "text": "Secret wording", "card": card, **extra}
    return envelope("inbox_message", language=language, telegram_user_id="777000", body=body)


@pytest.mark.parametrize(("category", "card"), INBOX_CASES)
@pytest.mark.parametrize("language", ["ru", "uk", "en"])
def test_an_inbox_message_with_the_sender_is_plain_text(telegram, make_channel, language, category, card):
    result = make_channel().deliver(snapshot("inbox_message", inbox_facts(language, category, card, sender=SENDER)))
    assert result is SENT
    [call] = telegram.calls_to("sendMessage")
    lines = call.params["text"].splitlines()
    assert lines[1] == f"{SENDER_LABELS[language]} Anna (@anna_k)"
    assert lines[2] == f"{NUMBER_LABELS[language]} {INBOX_ID}"
    assert lines[3:] == ["", "Secret wording"]
    assert "parse_mode" not in call.params


@pytest.mark.parametrize("language", ["ru", "uk", "en"])
def test_a_complaint_has_one_web_app_button_to_its_card(telegram, make_channel, language):
    result = make_channel().deliver(snapshot("inbox_message", inbox_facts(language, "complaint", CARD, sender=SENDER)))
    assert result is SENT
    [call] = telegram.calls_to("sendMessage")
    # The same configured address, with the card id in the query (answer Q20, variant A).
    assert call.json("reply_markup") == {"inline_keyboard": [[{
        "text": CARD_LABELS[language], "web_app": {"url": f"{WEB_APP_URL}?card={CARD['card_id']}"}}]]}


@pytest.mark.parametrize("language", ["ru", "uk", "en"])
def test_feedback_without_a_card_has_no_button(telegram, make_channel, language):
    result = make_channel().deliver(snapshot("inbox_message", inbox_facts(language, "feedback", None, sender=SENDER)))
    assert result is SENT
    [call] = telegram.calls_to("sendMessage")
    assert "reply_markup" not in call.params


def test_a_complaint_whose_card_id_is_no_uuid_fails_before_any_network_call(telegram, make_channel):
    card = {"card_id": "../settings?x=1", "lemma": "Haus"}
    result = make_channel().deliver(snapshot("inbox_message", inbox_facts("en", "complaint", card)))
    assert result.reason == "pre_send_error"
    assert telegram.calls_to("sendMessage") == []


@pytest.mark.parametrize(("category", "card"), INBOX_CASES)
def test_an_old_inbox_body_without_the_sender_is_still_delivered(telegram, make_channel, category, card):
    result = make_channel().deliver(snapshot("inbox_message", inbox_facts("en", category, card)))
    assert result is SENT
    [call] = telegram.calls_to("sendMessage")
    assert f"Message number: {INBOX_ID}" in call.params["text"] and "From:" not in call.params["text"]


@pytest.fixture
def library_levels():
    levels = {name: logging.getLogger(name).level for name in logs.LIBRARY_LOGGERS}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("script", [
    lambda t: t.fail("sendMessage", 400, "Bad Request"),
    lambda t: t.fail("sendMessage", 429, "Too Many Requests", retry_after=3),
    transport(httpx.ConnectError),
], ids=["400", "429", "connect-error"])
@pytest.mark.parametrize("sender", [SENDER, {"first_name": 7, "username": "anna_k"}], ids=["sent", "bad-sender"])
def test_the_log_of_an_inbox_delivery_has_no_name_username_or_text(telegram, make_channel, caplog, library_levels,
                                                                  script, sender):
    channel = make_channel()
    script(telegram)
    # As in the running process (logs.private_logs): PTB and httpx stay at WARNING, since at DEBUG
    # they write the texts and names they send.
    logs.quiet_libraries()
    with caplog.at_level("DEBUG"):
        channel.deliver(snapshot("inbox_message", inbox_facts("ru", "feedback", None, sender=sender)))
    assert caplog.text
    assert not any(value in caplog.text for value in ("Anna", "anna_k", "Secret wording", "777000"))


@pytest.mark.parametrize("script", [
    lambda t: t.fail("sendMessage", 400, "Bad Request"),
    lambda t: t.fail("sendMessage", 429, "Too Many Requests", retry_after=3),
    transport(httpx.ConnectError),
], ids=["400", "429", "connect-error"])
def test_the_log_of_a_complaint_delivery_has_no_card_id_or_address(telegram, make_channel, caplog, library_levels,
                                                                   script):
    channel = make_channel()
    script(telegram)
    logs.quiet_libraries()
    with caplog.at_level("DEBUG"):
        channel.deliver(snapshot("inbox_message", inbox_facts("ru", "complaint", CARD, sender=SENDER)))
    assert caplog.text
    assert not any(value in caplog.text for value in (CARD["card_id"], "card=", WEB_APP_URL, "Haus"))
