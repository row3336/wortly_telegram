"""The study_reminder template on the facts envelope of the core's builder (T-D §4.3; C-JOBS-KINDS).

The body is the one the core's builder writes (wortly.jobs.builders.study_reminder):
local date, pair and the ready items with the Today numbers. The text is the owner's
(S06): two lines split by a real line break, without the old share of the group and the
old chat-menu line. The message has the plain Mini App button with the S04 label.
"""

from __future__ import annotations

import json

import pytest

from wortly_telegram.messages import LANGUAGES, OutgoingMessage, RenderError
from wortly_telegram.messages.registry import discover
from wortly_telegram.messages.study_reminder import APP_BUTTON_LABELS, TEMPLATE
from wortly_telegram.webapp import BUTTON_LABELS, keyboard


ADDRESS = "https://app.example.test/wortly/"
LABELS = {"ru": "Открыть приложение", "uk": "Відкрити застосунок", "en": "Open app"}
EXPECTED = {"ru": "Напоминание об изучении.\nГотово карточек: 3",
            "uk": "Нагадування про вивчення.\nГотово карток: 3",
            "en": "Learning reminder.\nCards ready: 3"}


def facts(language="ru", body=None):
    return json.dumps({"schema": 1, "kind": "study_reminder", "ui_language": language,
                       "recipient": {"telegram_user_id": "4242"},
                       "body": body or {"local_date": "2026-10-08", "pair": "ru-de",
                                        "items": [{"type": "learning_ready", "ready": 3}]}})


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_learning_reminder_is_the_owners_two_line_text(language):
    prepared = discover().prepare("study_reminder", facts(language))
    assert prepared.chat_id == 4242
    assert prepared.message.text == EXPECTED[language]
    assert prepared.message.text.splitlines() == EXPECTED[language].split("\n")


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_text_has_no_markup_no_share_of_the_group_and_no_old_menu_line(language):
    # S06: a real line break, not a literal <br>; neither "need N" (П14), nor "= N", nor the menu path.
    text = discover().prepare("study_reminder", facts(language)).message.text
    assert "<" not in text and ">" not in text
    assert "=" not in text and "/" not in text
    assert not any(word in text.lower() for word in ("нужно", "потрібно", "need", "меню", "menu"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_reminder_has_one_plain_mini_app_button_with_its_label(language):
    # S04: the usual entry, the configured address as is; nothing in it starts or ends a session.
    message = discover().prepare("study_reminder", facts(language)).message
    assert message.app_button is True and message.buttons == () and message.app_card is None
    assert APP_BUTTON_LABELS == LABELS
    [[button]] = keyboard(message, ADDRESS, language).inline_keyboard
    assert button.text == LABELS[language]
    assert button.web_app.url == ADDRESS
    assert button.callback_data is None and button.url is None


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_start_button_keeps_its_label(language):
    [[button]] = keyboard(OutgoingMessage("text", app_button=True), ADDRESS, language).inline_keyboard
    assert button.text == BUTTON_LABELS[language] and button.web_app.url == ADDRESS


@pytest.mark.parametrize("label", ["", "  ", 5])
def test_a_button_label_needs_a_text_and_the_mini_app_button(label):
    with pytest.raises(RenderError):
        OutgoingMessage("text", app_button=True, app_label=label)
    with pytest.raises(RenderError):
        OutgoingMessage("text", app_label="Open app")


@pytest.mark.parametrize("body", [
    {"local_date": "2026-10-08", "pair": "ru-de", "items": []},
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": "review_due", "ready": 2}]},
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": "learning_ready", "ready": 0}]},
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": "learning_ready", "ready": True}]},
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": "learning_ready", "ready": 2, "x": 1}]},
    {"local_date": "2026-10-08", "pair": "ru-de", "items": ["learning_ready"]},
    {"local_date": "2026-10-08", "items": [{"type": "learning_ready", "ready": 2}]},
    {"local_date": None, "pair": "ru-de", "items": [{"type": "learning_ready", "ready": 2}]},
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": "learning_ready", "ready": 2}], "more": 1},
], ids=["no-items", "unknown-type", "zero", "bool", "extra-item-field", "item-not-object", "no-pair",
        "no-date", "extra-field"])
def test_faulty_bodies_fail_before_any_network_call(body):
    with pytest.raises(RenderError):
        discover().prepare("study_reminder", facts(body=body))


def test_the_template_renders_its_examples_in_every_language():
    for body in TEMPLATE.examples:
        for language in LANGUAGES:
            assert TEMPLATE.render(body, language).text.strip()
