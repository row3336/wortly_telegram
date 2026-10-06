"""/start: text and the web_app button with the configured address; language; subject; chat kinds."""

from __future__ import annotations

import inspect

import pytest

from conftest import feed
from fakes.telegram_api import WEB_APP_URL, message_update, user
from wortly_telegram.app import build_application
from wortly_telegram.start import TEXTS
from wortly_telegram.webapp import WebAppAddressError


async def test_start_answers_with_the_mini_app_button(telegram, make_application):
    application = await make_application()
    await feed(application, message_update("/start", from_user=user(5005, language_code="ru")))
    [call] = telegram.calls_to("sendMessage")
    assert call.params["chat_id"] == "5005"
    assert call.params["text"] == TEXTS["ru"]
    assert call.json("reply_markup") == {"inline_keyboard": [[{"text": "Старт", "web_app": {"url": WEB_APP_URL}}]]}


async def test_the_button_opens_the_configured_address(telegram, make_application):
    application = await make_application(web_app_url="https://other.example.test/mini")
    await feed(application, message_update("/start", from_user=user(5005)))
    [call] = telegram.calls_to("sendMessage")
    [[button]] = call.json("reply_markup")["inline_keyboard"]
    assert button["web_app"] == {"url": "https://other.example.test/mini"}


def test_there_is_no_default_mini_app_address():
    parameter = inspect.signature(build_application).parameters["web_app_url"]
    assert parameter.default is inspect.Parameter.empty


async def test_a_non_https_address_stops_the_build(telegram, gateway):
    with pytest.raises(WebAppAddressError):
        build_application(telegram.bot(), gateway=gateway, web_app_url="http://app.example.test/")


async def test_the_subject_is_the_sender_of_the_update(telegram, gateway, make_application):
    application = await make_application()
    await feed(application, message_update("/start 99999", from_user=user(5005), chat_id=5005))
    [call] = gateway.calls
    assert call.operation == "profile_summary"
    assert call.principal.external_id == "5005"


@pytest.mark.parametrize(("summary", "client", "expected"), [
    ({"role": "user", "status": "admitted", "ui_language": "uk"}, "ru", "uk"),
    ({"role": "user", "status": "pending", "ui_language": None}, "ru-RU", "ru"),
    (None, "en-GB", "en"),
    (None, None, "en"),
    (None, "de", "en"),
])
async def test_the_reply_language(telegram, gateway, make_application, summary, client, expected):
    gateway.summaries["5005"] = summary
    application = await make_application()
    await feed(application, message_update("/start", from_user=user(5005, language_code=client)))
    [call] = telegram.calls_to("sendMessage")
    assert call.params["text"] == TEXTS[expected]


async def test_without_the_core_the_reply_uses_the_client_language(telegram, gateway, make_application):
    gateway.error = RuntimeError("database unavailable")
    application = await make_application()
    await feed(application, message_update("/start", from_user=user(5005, language_code="uk")))
    [call] = telegram.calls_to("sendMessage")
    assert call.params["text"] == TEXTS["uk"]


async def test_start_in_a_group_is_not_answered(telegram, make_application):
    application = await make_application()
    await feed(application, message_update("/start", from_user=user(5005), chat_id=-100500, chat_type="supergroup"))
    assert telegram.calls_to("sendMessage") == []


async def test_an_edited_start_is_not_answered(telegram, gateway, make_application):
    application = await make_application()
    await feed(application, message_update("/start", from_user=user(5005), edited=True))
    assert telegram.calls_to("sendMessage") == []
    assert gateway.calls == []


async def test_the_bot_application_has_no_rate_limiter(make_application):
    application = await make_application()
    assert getattr(application.bot, "rate_limiter", None) is None
