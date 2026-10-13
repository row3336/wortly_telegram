"""Quiet sending: OutgoingMessage.silent reaches the Bot API as disable_notification, per message only.

Local fake transport only; no network. A test-only template stands in for the future weekly report.
"""

from __future__ import annotations

import pytest

from conftest import TELEGRAM_SETTINGS
from fakes.jobs import envelope, snapshot
from fakes.telegram_api import WEB_APP_URL
from wortly.jobs.channel import SENT
from wortly_telegram.app import make_bot
from wortly_telegram.delivery.channel import TelegramChannel
from wortly_telegram.delivery.main import loop_runner
from wortly_telegram.messages import OutgoingMessage, RenderError, Template
from wortly_telegram.messages.registry import TemplateRegistry, discover


class IdleCapacity:
    def pause(self, duration) -> None:
        pass


def quiet_template(kind: str, **message) -> Template:
    return Template(kind, lambda body, language: OutgoingMessage("Quiet text", **message), examples=({},))


@pytest.fixture
def channel_with(telegram, bot_loop):
    run = loop_runner(bot_loop)
    bots = []

    def make(*templates: Template) -> TelegramChannel:
        bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
        run(bot.initialize())
        bots.append(bot)
        registry = TemplateRegistry([*discover()._templates.values(), *templates])
        return TelegramChannel(bot, templates=registry, web_app_url=WEB_APP_URL, capacity=IdleCapacity(), run=run)

    yield make
    for bot in bots:
        run(bot.shutdown())


def test_silent_defaults_to_false():
    assert OutgoingMessage("text").silent is False


@pytest.mark.parametrize("value", [1, 0, "true", None, "", 1.0])
def test_a_silent_value_that_is_no_bool_is_rejected(value):
    with pytest.raises(RenderError):
        OutgoingMessage("text", silent=value)


def test_an_existing_kind_sends_the_same_call_as_before(telegram, channel_with):
    result = channel_with().deliver(snapshot(facts=envelope(language="uk", telegram_user_id="777000")))
    assert result is SENT
    [call] = telegram.calls_to("sendMessage")
    assert "disable_notification" not in call.params
    assert sorted(call.params) == ["chat_id", "reply_markup", "text"]


def test_an_explicit_silent_false_sends_the_same_call_as_the_default(telegram, channel_with):
    channel = channel_with(quiet_template("x_loud", silent=False), quiet_template("x_default"))
    for kind in ("x_loud", "x_default"):
        assert channel.deliver(snapshot(kind, envelope(kind, telegram_user_id="777000"))) is SENT
    first, second = telegram.calls_to("sendMessage")
    assert first.params == second.params
    assert "disable_notification" not in first.params


def test_a_silent_message_carries_disable_notification(telegram, channel_with):
    channel = channel_with(quiet_template("x_quiet", silent=True))
    assert channel.deliver(snapshot("x_quiet", envelope("x_quiet", telegram_user_id="777000"))) is SENT
    [call] = telegram.calls_to("sendMessage")
    assert call.params["disable_notification"] == "true"
    assert call.params["chat_id"] == "777000" and call.params["text"] == "Quiet text"
    assert "parse_mode" not in call.params


def test_silence_is_per_message_not_sticky(telegram, channel_with):
    channel = channel_with(quiet_template("x_quiet", silent=True))
    channel.deliver(snapshot("x_quiet", envelope("x_quiet", telegram_user_id="777000")))
    channel.deliver(snapshot(facts=envelope(telegram_user_id="777000")))
    quiet, loud = telegram.calls_to("sendMessage")
    assert "disable_notification" in quiet.params and "disable_notification" not in loud.params


def test_a_silent_message_keeps_its_keyboard(telegram, channel_with):
    channel = channel_with(quiet_template("x_quiet", silent=True, app_button=True))
    channel.deliver(snapshot("x_quiet", envelope("x_quiet", telegram_user_id="777000")))
    [call] = telegram.calls_to("sendMessage")
    assert call.params["disable_notification"] == "true"
    assert call.json("reply_markup")["inline_keyboard"][0][0]["web_app"] == {"url": WEB_APP_URL}


@pytest.mark.parametrize(("script", "outcome", "reason"), [
    (lambda t: t.fail("sendMessage", 429, "Too Many Requests", retry_after=30), "known_failed", "rate_limited"),
    (lambda t: t.fail("sendMessage", 403, "Forbidden: bot was blocked by the user"), "known_failed", "blocked"),
    (lambda t: t.fail("sendMessage", 400, "Bad Request"), "known_failed", "rejected"),
    (lambda t: t.fail("sendMessage", 401, "Unauthorized"), "known_failed", "channel_config"),
    (lambda t: t.fail("sendMessage", 500, "Internal"), "unknown", None),
], ids=["429", "403-blocked", "400", "401", "500"])
def test_a_silent_failure_is_classified_like_a_loud_one(telegram, channel_with, script, outcome, reason):
    channel = channel_with(quiet_template("x_quiet", silent=True))
    script(telegram)
    result = channel.deliver(snapshot("x_quiet", envelope("x_quiet", telegram_user_id="777000")))
    assert len(telegram.calls_to("sendMessage")) == 1
    assert (result.outcome, result.reason) == (outcome, reason)
