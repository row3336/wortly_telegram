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

SENDER = {"first_name": "Anna", "username": "anna_k"}
SENDER_LABELS = {"ru": "От:", "uk": "Від:", "en": "From:"}
NUMBER_LABELS = {"ru": "Номер обращения:", "uk": "Номер звернення:", "en": "Message number:"}

COMPLAINT_HEADINGS = {"ru": "⚠️ Жалоба на карточку «Haus»", "uk": "⚠️ Скарга на картку «Haus»",
                      "en": "⚠️ Complaint about the card “Haus”"}
FEEDBACK_HEADINGS = {"ru": "✉️ Сообщение разработчику", "uk": "✉️ Повідомлення розробнику",
                     "en": "✉️ Message to the developer"}


def body(category: str = "feedback", text: str = "Hello", card: object = None, **extra: object) -> dict:
    return {"inbox_id": INBOX_ID, "category": category, "text": text, "card": card, **extra}


def number_line(language: str) -> str:
    return f"{NUMBER_LABELS[language]} {INBOX_ID}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_complaint_names_the_card_and_carries_the_text(language):
    message = TEMPLATE.render(body("complaint", "Wrong plural", CARD), language)
    assert message.text == f"{COMPLAINT_HEADINGS[language]}\n{number_line(language)}\n\nWrong plural"
    # The card button (answer Q20): the card id only, never the general Mini App button.
    assert message.app_card == CARD["card_id"]
    assert message.app_button is False and message.buttons == ()


@pytest.mark.parametrize("language", LANGUAGES)
def test_feedback_has_no_card_and_carries_the_text(language):
    message = TEMPLATE.render(body("feedback", "Nice app"), language)
    assert message.text == f"{FEEDBACK_HEADINGS[language]}\n{number_line(language)}\n\nNice app"
    assert message.app_card is None
    assert message.app_button is False and message.buttons == ()


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
    assert message == (COMPLAINT_HEADINGS[language].replace("Haus", card["lemma"])
                       + f"\n{number_line(language)}\n\nx")


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


@pytest.mark.parametrize(("category", "card", "headings"), [
    ("complaint", CARD, COMPLAINT_HEADINGS), ("feedback", None, FEEDBACK_HEADINGS)])
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_sender_block_has_the_name_the_username_and_the_full_number(language, category, card, headings):
    message = TEMPLATE.render(body(category, "Hello", card, sender=SENDER), language)
    assert message.text == (f"{headings[language]}\n{SENDER_LABELS[language]} Anna (@anna_k)\n"
                            f"{number_line(language)}\n\nHello")
    # The full number, all 36 characters, never a prefix of it.
    assert INBOX_ID in message.text and len(INBOX_ID) == 36


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(("sender", "shown"), [
    ({"first_name": "Anna"}, "Anna"),
    ({"username": "anna_k"}, "@anna_k"),
    ({"first_name": "Anna", "username": ""}, "Anna"),
    ({"first_name": "  ", "username": "anna_k"}, "@anna_k"),
], ids=["name-only", "username-only", "blank-username", "blank-name"])
def test_the_sender_line_shows_only_what_the_core_sent(language, sender, shown):
    text = TEMPLATE.render(body(sender=sender), language).text
    assert text.splitlines()[1] == f"{SENDER_LABELS[language]} {shown}"
    # No username in the facts: nothing is made up (no "@", no placeholder).
    assert ("@" in text) is ("@anna_k" in shown)


@pytest.mark.parametrize("sender", [{}, {"first_name": "", "username": " "}], ids=["empty", "blank"])
@pytest.mark.parametrize("language", LANGUAGES)
def test_a_sender_fact_that_names_nobody_adds_no_sender_line(language, sender):
    text = TEMPLATE.render(body(sender=sender), language).text
    assert text == f"{FEEDBACK_HEADINGS[language]}\n{number_line(language)}\n\nHello"


@pytest.mark.parametrize("category", ["complaint", "feedback"])
@pytest.mark.parametrize("language", LANGUAGES)
def test_an_old_body_without_the_sender_is_delivered_with_the_number(language, category):
    # A job made before the core sent the sender: no sender line, no error, the number is in the body.
    card = CARD if category == "complaint" else None
    text = TEMPLATE.render(body(category, "Hello", card), language).text
    assert SENDER_LABELS[language] not in text
    assert number_line(language) in text and text.endswith("\n\nHello")


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_sender_values_within_their_bounds_are_output_as_they_are_without_markup(language):
    # 128 and 32 UTF-16 units are the bounds of R7-01; the cut beyond them: test_messages_inbox_bounds.
    sender = {"first_name": "<b>A</b> *x* _y_ [z](u) " + "ы" * 104, "username": "a_b" + "c" * 29}
    assert len(sender["first_name"]) == 128 and len(sender["username"]) == 32
    text = TEMPLATE.render(body(sender=sender), language).text
    assert text.splitlines()[1] == f"{SENDER_LABELS[language]} {sender['first_name']} (@{sender['username']})"


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_sender_block_adds_no_button(language):
    message = TEMPLATE.render(body("complaint", "x", CARD, sender=SENDER), language)
    assert message.app_button is False and message.buttons == ()
    assert message.app_card == CARD["card_id"]
    assert TEMPLATE.render(body("feedback", "x", sender=SENDER), language).app_card is None


@pytest.mark.parametrize("card_id", ["not-a-card", "5D1A7C3E-0000-4000-8000-000000000002",
                                     "5d1a7c3e000040008000000000000002", "5d1a7c3e-0000-4000-8000-000000000002&x=1"])
def test_a_complaint_card_id_that_is_no_canonical_uuid_is_refused(card_id):
    with pytest.raises(RenderError) as error:
        TEMPLATE.render(body("complaint", "x", {"card_id": card_id, "lemma": "Haus"}), "en")
    assert card_id not in str(error.value)


@pytest.mark.parametrize("bad", [
    None, "Anna", ["Anna"], {"surname": "K"}, {"first_name": None}, {"first_name": 5}, {"username": ["a"]},
    {"first_name": "Anna", "extra": "x"},
], ids=["none", "string", "list", "surname", "name-none", "name-int", "username-list", "extra-key"])
def test_a_faulty_sender_fact_fails_before_any_network_call(bad):
    with pytest.raises(RenderError) as error:
        TEMPLATE.render(body(sender=bad), "en")
    assert "Anna" not in str(error.value)


def test_the_registry_finds_the_kind():
    assert "inbox_message" in discover().kinds
