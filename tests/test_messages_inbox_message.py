"""The inbox_message template: both kinds, three languages, the user's text as plain text."""

from __future__ import annotations

import json

import pytest

from wortly_telegram.messages import LANGUAGES, RenderError
from wortly_telegram.messages.inbox_message import TEMPLATE
from wortly_telegram.messages.registry import discover


INBOX_ID = "5d1a7c3e-0000-4000-8000-000000000001"
CARD = {"card_id": "5d1a7c3e-0000-4000-8000-000000000002", "lemma": "Haus"}
# What a user may type: HTML, Markdown, MarkdownV2 specials, an entity and a link.
MARKUP = '<b>fett</b> *kursiv* _x_ `code` [a](https://example.test) &amp; \\ <script>1</script>'

COMPLAINT_HEADINGS = {"ru": "⚠️ Жалоба на карточку «Haus»", "uk": "⚠️ Скарга на картку «Haus»",
                      "en": "⚠️ Complaint about the card “Haus”"}
FEEDBACK_HEADINGS = {"ru": "✉️ Сообщение разработчику", "uk": "✉️ Повідомлення розробнику",
                     "en": "✉️ Message to the developer"}


def body(category: str = "feedback", text: str = "Hello", card: object = None) -> dict:
    return {"inbox_id": INBOX_ID, "category": category, "text": text, "card": card}


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_complaint_names_the_card_and_carries_the_text(language):
    message = TEMPLATE.render(body("complaint", "Wrong plural", CARD), language)
    assert message.text == f"{COMPLAINT_HEADINGS[language]}\n\nWrong plural"
    assert message.app_button is False


@pytest.mark.parametrize("language", LANGUAGES)
def test_feedback_has_no_card_and_carries_the_text(language):
    message = TEMPLATE.render(body("feedback", "Nice app"), language)
    assert message.text == f"{FEEDBACK_HEADINGS[language]}\n\nNice app"
    assert message.app_button is False


@pytest.mark.parametrize(("category", "card"), [("complaint", CARD), ("feedback", None)])
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_user_text_is_output_as_it_is_without_markup(language, category, card):
    message = TEMPLATE.render(body(category, MARKUP, card), language).text
    assert message.endswith("\n\n" + MARKUP)
    # The template adds no markup of its own around the user's text or the lemma.
    heading = message[: -len(MARKUP)]
    assert not set(heading) & set("<>*_`[]&\\")


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_lemma_is_output_as_it_is_too(language):
    card = {"card_id": CARD["card_id"], "lemma": "<i>Haus</i> & [x](y)"}
    message = TEMPLATE.render(body("complaint", "x", card), language).text
    assert message == COMPLAINT_HEADINGS[language].replace("Haus", card["lemma"]) + "\n\nx"


@pytest.mark.parametrize("category", ["complaint", "feedback"])
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_text_keeps_its_blanks_and_line_breaks_at_the_edges(language, category):
    # What the user typed is shown as it is; the template does not tidy it up.
    padded = "  \n first line\n\nlast line \t\n "
    message = TEMPLATE.render(body(category, padded, CARD if category == "complaint" else None), language).text
    assert message.endswith("\n\n" + padded)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_channel_text_is_plain_after_prepare(language):
    facts = json.dumps({"schema": 1, "kind": "inbox_message", "ui_language": language,
                        "recipient": {"telegram_user_id": "4242"}, "body": body("feedback", MARKUP)})
    prepared = discover().prepare("inbox_message", facts)
    assert prepared.chat_id == 4242
    assert prepared.message.text.endswith(MARKUP)
    assert prepared.message.app_button is False


def test_a_long_text_is_neither_cut_nor_split():
    # Current behaviour, not a decision: the bound of the user's text is the owner's open question 2.19а.
    long_text = "ы" * 5000
    message = TEMPLATE.render(body("feedback", long_text), "ru").text
    assert message.endswith(long_text)
    assert len(message) > 4096


@pytest.mark.parametrize("bad", [
    {"category": "other"},
    {"category": "other", "card": None},
    {"category": "complaint", "card": None},
    {"category": "feedback", "card": CARD},
    {"category": "complaint", "card": {"card_id": CARD["card_id"]}},
    {"category": "complaint", "card": {**CARD, "extra": 1}},
    {"category": "complaint", "card": {"card_id": CARD["card_id"], "lemma": " "}},
    {"category": "complaint", "card": {**CARD, "card_id": ""}},
    {"category": "complaint", "card": {**CARD, "card_id": None}},
    {"text": ""},
    {"text": "   "},
    {"text": None},
    {"inbox_id": None},
    {"unexpected": 1},
], ids=["category", "category-no-card", "complaint-no-card", "feedback-with-card", "card-no-lemma", "card-extra",
        "lemma-blank", "card-id-empty", "card-id-none", "text-empty", "text-blank", "text-none", "inbox-id",
        "extra-fact"])
def test_faulty_facts_fail_before_any_network_call(bad):
    facts = body("complaint", "x", CARD) | bad
    with pytest.raises(RenderError):
        TEMPLATE.render(facts, "en")


def test_missing_facts_fail():
    with pytest.raises(RenderError):
        TEMPLATE.render({}, "en")


def test_the_registry_finds_the_kind():
    assert "inbox_message" in discover().kinds
