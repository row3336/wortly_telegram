"""Template of the inbox_message job: a message to the developer, shown to the owner (К07-051, К07-052, К07-099; T-D §4.3, §7.2).

The core's builder (wortly.jobs.builders.inbox_message) sends the facts of one inbox
record: its category, the user's text and the card the text names. A message about a
card is a complaint and names the card's lemma; one without a card is feedback.

The user's text is untrusted. The channel sends plain text without parse_mode (ВП-16),
so it is placed as it is: nothing escapes it and nothing here may add markup around it.
The text is not cut or split: the channel makes one sendMessage call, and a message over
Telegram's 4096 characters is refused there (BadRequest, a known failure of the attempt).
The upper bound of a user's text is an open question to the owner (2.19а).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Language, OutgoingMessage, RenderError, Template


KIND = "inbox_message"
COMPLAINT = "complaint"
FEEDBACK = "feedback"

_FIELDS = frozenset({"inbox_id", "category", "text", "card"})
_CARD_FIELDS = frozenset({"card_id", "lemma"})

# WORKING TEXT (not set by the specification): the headings of the two kinds; the complaint
# heading has the card's lemma in {lemma}. The old bot has only the labels (OLD bot/i18n.py
# "inbox_reports", "inbox_feedback") and no message template to copy; final wording waits
# for the owner (2.19/Q20).
HEADINGS: dict[str, dict[Language, str]] = {
    COMPLAINT: {
        "ru": "⚠️ Жалоба на карточку «{lemma}»",
        "uk": "⚠️ Скарга на картку «{lemma}»",
        "en": "⚠️ Complaint about the card “{lemma}”",
    },
    FEEDBACK: {
        "ru": "✉️ Сообщение разработчику",
        "uk": "✉️ Повідомлення розробнику",
        "en": "✉️ Message to the developer",
    },
}


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise RenderError(f"The inbox_message {name} must be a non-empty string")
    return value


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    if set(body) != _FIELDS:
        raise RenderError("The inbox_message body has unexpected facts")
    category, text, card = body["category"], _text(body["text"], "text"), body["card"]
    _text(body["inbox_id"], "inbox_id")
    if category == COMPLAINT:
        if not isinstance(card, Mapping) or set(card) != _CARD_FIELDS:
            raise RenderError("A complaint names the card")
        _text(card["card_id"], "card_id")
        heading = HEADINGS[COMPLAINT][language].format(lemma=_text(card["lemma"], "lemma"))
    elif category == FEEDBACK:
        if card is not None:
            raise RenderError("A message without a complaint names no card")
        heading = HEADINGS[FEEDBACK][language]
    else:
        raise RenderError("The inbox_message category is unknown")
    return OutgoingMessage(f"{heading}\n\n{text}")


TEMPLATE = Template(KIND, render, examples=(
    {"inbox_id": "5d1a7c3e-0000-4000-8000-000000000001", "category": COMPLAINT,
     "text": "The plural is wrong.", "card": {"card_id": "5d1a7c3e-0000-4000-8000-000000000002", "lemma": "Haus"}},
    {"inbox_id": "5d1a7c3e-0000-4000-8000-000000000003", "category": FEEDBACK,
     "text": "Thank you for the app.", "card": None},
))
