"""The study_reminder template on the facts envelope of the core's builder (T-D §4.3; C-JOBS-KINDS).

The body is the one the core's builder writes (wortly.jobs.builders.study_reminder):
local date, pair and the ready items with the Today numbers. The text is a working text
pending the owner's approval: the old bot's Learning reminder without its quorum part
and its chat-menu line.
"""

from __future__ import annotations

import json

import pytest

from wortly_telegram.messages import LANGUAGES, RenderError
from wortly_telegram.messages.registry import discover
from wortly_telegram.messages.study_reminder import TEMPLATE


EXPECTED = {"ru": "Напоминание об изучении:\nготово сейчас = 3",
            "uk": "Нагадування про вивчення:\nготово зараз = 3",
            "en": "Learning reminder:\ndue now = 3"}


def facts(language="ru", body=None):
    return json.dumps({"schema": 1, "kind": "study_reminder", "ui_language": language,
                       "recipient": {"telegram_user_id": "4242"},
                       "body": body or {"local_date": "2026-10-08", "pair": "ru-de",
                                        "items": [{"type": "learning_ready", "ready": 3}]}})


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_learning_reminder_is_the_old_text_without_quorum_and_menu_line(language):
    prepared = discover().prepare("study_reminder", facts(language))
    assert prepared.chat_id == 4242
    assert prepared.message.text == EXPECTED[language]
    assert prepared.message.app_button is False and prepared.message.buttons == ()


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
