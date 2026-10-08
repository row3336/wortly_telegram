"""The access_request template on the facts of the core's own builder (answer 2.6 а, б, в; C-ADAPTER-3).

The real builder (wortly.jobs.builders.access_request) runs over scripted rows instead
of a database, so these tests hold in both interpreters (design §9.5): the template
renders exactly what the builder writes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from conftest import TELEGRAM_SETTINGS
from fakes.core_gateway import JOB_EVENT_QUERY, RECIPIENT_QUERY, ScriptedFacts, recipient_row
from fakes.jobs import snapshot
from fakes.telegram_api import WEB_APP_URL
from wortly.jobs.builders import access_request as builder
from wortly.jobs.channel import SENT
from wortly.jobs.models import JobContext
from wortly_telegram.app import make_bot
from wortly_telegram.delivery.channel import TelegramChannel
from wortly_telegram.delivery.main import loop_runner
from wortly_telegram.messages import CALLBACK_DATA_BYTES, LANGUAGES, CallbackButton, OutgoingMessage, RenderError
from wortly_telegram.messages.access_request import TEMPLATE, applicant_label
from wortly_telegram.messages.registry import discover


REQUESTED_AT = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
REQUEST_ID, APPLICANT_ID = uuid4(), uuid4()
OWNER_TELEGRAM_ID = "4242"

TEXTS = {"ru": "👤 ({label}) запрашивает доступ на использование бота",
         "uk": "👤 ({label}) запитує доступ до використання бота",
         "en": "👤 ({label}) requests access to the bot"}
LABELS = {"ru": (("✅ Разрешить", "❌ Отклонить"), ("✍️ Отклонить с причиной",)),
          "uk": (("✅ Дозволити", "❌ Відхилити"), ("✍️ Відхилити з причиною",)),
          "en": (("✅ Approve", "❌ Reject"), ("✍️ Reject with a reason",))}
# What a Telegram user may put into a first name or a username: markup is shown as it is.
MARKUP_NAME = '<b>Anna</b> *x* _y_ `z` [a](https://example.test) &amp;'


def built(language: str = "ru", *, first_name: str | None = "Anna", username: str | None = "anna_k",
          applicant_telegram_id: str | None = "5005", version: int = 1, checked_after=timedelta(minutes=1)):
    reader = ScriptedFacts(
        (JOB_EVENT_QUERY, [{"payload": builder.job_payload(REQUEST_ID, APPLICANT_ID), "available_at": REQUESTED_AT}]),
        (RECIPIENT_QUERY, [recipient_row(language=language, telegram_user_id=OWNER_TELEGRAM_ID)]),
        ("FROM access_requests r", [{"id": REQUEST_ID, "version": version, "created_at": REQUESTED_AT,
                                     "external_id": applicant_telegram_id, "first_name": first_name,
                                     "username": username}]),
    )
    owner_id = uuid4()
    context = JobContext(uuid4(), owner_id, builder.job_key(REQUEST_ID, owner_id), builder.KIND, 1,
                         REQUESTED_AT + checked_after)
    return builder.build(reader, context)


def prepared(language: str = "ru", **facts):
    return discover().prepare(builder.KIND, built(language, **facts).facts_json)


def keyboard(message) -> tuple:
    return tuple(tuple(button.label for button in row) for row in message.buttons)


def data(message) -> list[str]:
    return [button.data for row in message.buttons for button in row]


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_owner_gets_the_applicant_and_three_decision_buttons(language):
    result = prepared(language)
    assert result.chat_id == int(OWNER_TELEGRAM_ID)
    assert result.message.text == TEXTS[language].format(label="@anna_k | Anna | 5005")
    assert keyboard(result.message) == LABELS[language]
    assert result.message.app_button is False


def test_the_buttons_name_the_request_and_its_version_only():
    result = prepared(version=7)
    assert data(result.message) == [f"acc:a:{REQUEST_ID}:7", f"acc:r:{REQUEST_ID}:7", f"acc:w:{REQUEST_ID}:7"]
    for value in data(result.message):
        # §5.3 п.1, §7.1 п.3: references to objects, never the actor or the applicant.
        assert OWNER_TELEGRAM_ID not in value and "5005" not in value
        assert len(value.encode("utf-8")) <= CALLBACK_DATA_BYTES


def test_the_longest_callback_data_fits_telegram():
    result = prepared(version=9_223_372_036_854_775_807)
    assert max(len(value.encode("utf-8")) for value in data(result.message)) <= CALLBACK_DATA_BYTES


@pytest.mark.parametrize(("first_name", "username", "telegram_id", "label"), [
    ("Anna", "anna_k", "5005", "@anna_k | Anna | 5005"),
    ("Anna", None, "5005", "Anna | 5005"),
    (None, "anna_k", "5005", "@anna_k | 5005"),
    (None, None, "5005", "5005"),
    (None, None, None, "?"),
])
def test_the_label_has_the_parts_the_core_has(first_name, username, telegram_id, label):
    assert applicant_label(telegram_id, first_name, username) == label
    result = prepared(first_name=first_name, username=username, applicant_telegram_id=telegram_id)
    assert result.message.text == TEXTS["ru"].format(label=label)


@pytest.mark.parametrize("language", LANGUAGES)
def test_names_are_output_as_they_are_without_markup(language):
    result = prepared(language, first_name=MARKUP_NAME, username="anna_<i>")
    assert result.message.text == TEXTS[language].format(label=f"@anna_<i> | {MARKUP_NAME} | 5005")


def test_a_closed_job_leaves_nothing_to_render():
    # Answer 2.5, part 2: the notice lives 24 hours from the request; owner decision 2.19и makes
    # the boundary inclusive, so the job is closed only by a check after the 24th hour.
    assert built(checked_after=timedelta(hours=24, microseconds=1)) is None


def test_a_notice_checked_at_exactly_24_hours_is_still_built():
    assert built(checked_after=timedelta(hours=24)) is not None


@pytest.mark.parametrize("body", [
    {},
    {**TEMPLATE.examples[0], "extra": 1},
    {**TEMPLATE.examples[0], "request_version": 1},
    {**TEMPLATE.examples[0], "request_version": "0"},
    {**TEMPLATE.examples[0], "request_id": "not-a-uuid"},
    {**TEMPLATE.examples[0], "applicant": {"telegram_user_id": "1", "first_name": None}},
    {**TEMPLATE.examples[0], "applicant": {"telegram_user_id": 1, "first_name": None, "username": None}},
])
def test_facts_outside_the_contract_fail_before_any_network_call(body):
    with pytest.raises(RenderError):
        TEMPLATE.render(body, "ru")


def test_the_channel_sends_the_buttons_as_an_inline_keyboard(telegram, bot_loop):
    run = loop_runner(bot_loop)
    bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
    run(bot.initialize())
    try:
        channel = TelegramChannel(bot, templates=discover(), web_app_url=WEB_APP_URL, capacity=None, run=run)
        facts = json.loads(built("en").facts_json)
        assert channel.deliver(snapshot(builder.KIND, facts)) is SENT
    finally:
        run(bot.shutdown())
    [call] = telegram.calls_to("sendMessage")
    assert call.params["chat_id"] == OWNER_TELEGRAM_ID
    assert "parse_mode" not in call.params
    assert call.json("reply_markup")["inline_keyboard"] == [
        [{"text": "✅ Approve", "callback_data": f"acc:a:{REQUEST_ID}:1"},
         {"text": "❌ Reject", "callback_data": f"acc:r:{REQUEST_ID}:1"}],
        [{"text": "✍️ Reject with a reason", "callback_data": f"acc:w:{REQUEST_ID}:1"}],
    ]


# -- the message model the template relies on (messages/__init__.py) ----------------------

def test_callback_data_is_bounded_in_bytes_not_characters():
    CallbackButton("x", "a" * CALLBACK_DATA_BYTES)
    with pytest.raises(RenderError):
        CallbackButton("x", "a" * (CALLBACK_DATA_BYTES + 1))
    with pytest.raises(RenderError):
        CallbackButton("x", "я" * 33)  # 33 characters, 66 bytes
    with pytest.raises(RenderError):
        CallbackButton("x", "")


@pytest.mark.parametrize("label", ["", " ", None])
def test_a_button_needs_a_label(label):
    with pytest.raises(RenderError):
        CallbackButton(label, "acc:l")


def test_a_message_has_the_app_button_or_callback_buttons_not_both():
    button = CallbackButton("x", "acc:l")
    with pytest.raises(RenderError):
        OutgoingMessage("t", app_button=True, buttons=((button,),))
    with pytest.raises(RenderError):
        OutgoingMessage("t", buttons=((),))
    with pytest.raises(RenderError):
        OutgoingMessage("t", buttons=[(button,)])
    assert OutgoingMessage("t", buttons=((button,),)).buttons == ((button,),)
