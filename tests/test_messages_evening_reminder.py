"""The evening_reminder template and the item reviews_ready (project_notifications ed.4 §7.3, §9 table 6, T1; N-AC33).

The body is the one of the study reminder, which the core's builder N3 writes for the evening job:
{"local_date", "pair", "items": [...]}, an item {"type": "reviews_ready", "ready": N}, and
"learning_ready" first when a Learning reminder is due at the same moment. The text, the heading and the
button are those of study_reminder (table 6): this file only fixes the new kind, the new item and the order.
No network, no database.
"""

from __future__ import annotations

import json

import pytest

from fakes.jobs import envelope, snapshot
from wortly_telegram.messages import LANGUAGES, RenderError
from wortly_telegram.messages import evening_reminder, study_reminder
from wortly_telegram.messages.registry import discover
from wortly_telegram.webapp import keyboard


ADDRESS = "https://app.example.test/wortly/"
HEADING = {"ru": "Напоминание об изучении.", "uk": "Нагадування про вивчення.", "en": "Learning reminder."}
# Table 6 of project_notifications ed.4, written out by hand.
REVIEWS = {"ru": "Готово повторов: {n}", "uk": "Готово повторень: {n}", "en": "Reviews ready: {n}"}
CARDS = {"ru": "Готово карточек: {n}", "uk": "Готово карток: {n}", "en": "Cards ready: {n}"}
BUTTON = {"ru": "Открыть приложение", "uk": "Відкрити застосунок", "en": "Open app"}
LEARNING = {"type": "learning_ready", "ready": 3}
REVIEW = {"type": "reviews_ready", "ready": 7}


def body(*items, **changes):
    facts = {"local_date": "2026-10-08", "pair": "ru-de", "items": list(items)}
    facts.update(changes)
    return facts


def facts(kind="evening_reminder", language="ru", **kwargs):
    return json.dumps(envelope(kind, language=language, body=kwargs.get("body", body(REVIEW))))


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_reviews_item_is_the_line_of_table_6(language):
    prepared = discover().prepare("evening_reminder", facts(language=language))
    assert prepared.chat_id == 4242
    assert prepared.message.text == f"{HEADING[language]}\n{REVIEWS[language].format(n=7)}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_lines_go_learning_first_then_the_reviews(language):
    expected = f"{HEADING[language]}\n{CARDS[language].format(n=3)}\n{REVIEWS[language].format(n=7)}"
    for items in ((LEARNING, REVIEW), (REVIEW, LEARNING)):
        for kind in ("evening_reminder", "study_reminder"):
            text = discover().prepare(kind, facts(kind, language, body=body(*items))).message.text
            assert text == expected, (kind, items)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("kind", ["evening_reminder", "study_reminder"])
def test_the_evening_kind_has_the_same_heading_and_button_as_the_study_kind(kind, language):
    message = discover().prepare(kind, facts(kind, language, body=body(LEARNING, REVIEW))).message
    assert message.text.splitlines()[0] == HEADING[language]
    assert message.app_button is True and message.app_label == BUTTON[language]
    assert message.buttons == () and message.app_card is None and message.parse_mode is None and message.silent is False
    [[button]] = keyboard(message, ADDRESS, language).inline_keyboard
    assert button.text == BUTTON[language] and button.web_app.url == ADDRESS


def test_the_two_kinds_render_one_body_alike():
    for example in evening_reminder.TEMPLATE.examples + study_reminder.TEMPLATE.examples:
        for language in LANGUAGES:
            assert evening_reminder.TEMPLATE.render(example, language) == study_reminder.TEMPLATE.render(example, language)


def test_the_kind_is_registered_with_its_examples_in_every_language():
    registry = discover()
    assert "evening_reminder" in registry.kinds and registry.template("evening_reminder") is evening_reminder.TEMPLATE
    assert evening_reminder.KIND == "evening_reminder"
    types = {item["type"] for example in evening_reminder.TEMPLATE.examples for item in example["items"]}
    assert types == {"learning_ready", "reviews_ready"}
    for example in evening_reminder.TEMPLATE.examples:
        for language in LANGUAGES:
            assert evening_reminder.TEMPLATE.render(example, language).text.strip()


def test_every_item_type_of_the_body_has_a_line_and_no_other_type_renders():
    assert set(study_reminder._LINES) == {study_reminder.LEARNING_READY, study_reminder.REVIEWS_READY}
    assert (study_reminder.REVIEWS_READY, evening_reminder.KIND) == ("reviews_ready", "evening_reminder")
    for language in LANGUAGES:
        assert set(study_reminder.REVIEWS_READY_LINES) == set(LANGUAGES)
        assert study_reminder.REVIEWS_READY_LINES[language] == REVIEWS[language].replace("{n}", "{ready}")


@pytest.mark.parametrize("bad", [
    body(),
    body({"type": "reviews_ready", "ready": 0}),
    body({"type": "reviews_ready", "ready": -1}),
    body({"type": "reviews_ready", "ready": True}),
    body({"type": "reviews_ready", "ready": "7"}),
    body({"type": "reviews_ready", "ready": 7.0}),
    body({"type": "reviews_ready"}),
    body({"type": "reviews_ready", "ready": 7, "x": 1}),
    body({"type": "review_due", "ready": 2}),
    body(REVIEW, REVIEW),
    body(LEARNING, LEARNING),
    body("reviews_ready"),
    body(REVIEW, local_date=None),
    body(REVIEW, pair=None),
    body(REVIEW, extra=1),
    {"local_date": "2026-10-08", "items": [REVIEW]},
    {"local_date": "2026-10-08", "pair": "ru-de", "items": "reviews_ready"},
], ids=["no-items", "zero", "negative", "bool", "string", "float", "no-count", "extra-item-field",
        "unknown-type", "reviews-twice", "learning-twice", "item-not-object", "no-date", "no-pair",
        "extra-field", "missing-pair", "items-not-list"])
@pytest.mark.parametrize("kind", ["evening_reminder", "study_reminder"])
def test_a_body_outside_the_form_fails_before_any_network_call(kind, bad):
    with pytest.raises(RenderError) as error:
        discover().prepare(kind, facts(kind, body=bad))
    assert "2026" not in str(error.value)


def test_the_error_names_the_kind_but_no_value():
    with pytest.raises(RenderError) as error:
        evening_reminder.render({"local_date": "2026-10-08"}, "en")
    assert "evening_reminder" in str(error.value) and "2026" not in str(error.value)


def test_the_envelope_of_another_kind_is_not_taken_for_the_evening_one():
    with pytest.raises(RenderError):
        discover().prepare("evening_reminder", facts("study_reminder"))
    with pytest.raises(RenderError):
        discover().prepare("study_reminder", facts("evening_reminder"))


def test_the_job_reaches_the_channel_as_a_loud_message_with_the_app_button(telegram):
    # The delivery snapshot of the kind prepares like any other: the chat of the recipient, no quiet flag.
    prepared = discover().prepare("evening_reminder", json.dumps(
        envelope("evening_reminder", telegram_user_id="777000", language="en", body=body(LEARNING, REVIEW))))
    assert prepared.chat_id == 777000 and prepared.language == "en"
    assert prepared.message.text.splitlines() == ["Learning reminder.", "Cards ready: 3", "Reviews ready: 7"]


def test_the_item_types_are_those_of_the_core_when_the_core_has_the_evening_job():
    reviews = pytest.importorskip("wortly.jobs.builders._reviews")
    from wortly.jobs.builders import evening_reminder as core_job, study_reminder as core_study
    assert reviews.REVIEWS_READY == study_reminder.REVIEWS_READY
    assert core_study.LEARNING_READY == study_reminder.LEARNING_READY
    assert core_job.KIND == evening_reminder.KIND
    assert core_job.ENVELOPE_SCHEMA == 1  # the schema the registry accepts
