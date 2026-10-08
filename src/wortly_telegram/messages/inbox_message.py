"""Template of the inbox_message job: a message to the developer, shown to the owner (К07-051, К07-052, К07-099; T-D §4.3, §7.2).

The core's builder (wortly.jobs.builders.inbox_message) sends the facts of one inbox
record: its category, the user's text and the card the text names. A message about a
card is a complaint and names the card's lemma; one without a card is feedback.

The owner also sees who wrote (answer Q20, 4.5а-Н2): the sender's first name and Telegram
username when the core has them, and the full number of the inbox record. The surname is
not stored and not shown. The core's ``sender`` fact is optional: a job enqueued before it
existed, or a sender without a stored name, is delivered with the number and no sender
line. The template cuts and shortens none of these values; their length is not decided.

The user's text, the first name and the username are untrusted. The channel sends plain
text without parse_mode (ВП-16), so they are placed as they are: nothing escapes them and
nothing here may add markup around them.

A complaint carries one button that opens its card in the Mini App (answer Q20): the
message names the card id, the channel builds the web_app button (webapp.card_keyboard).
A message without a card has no button.
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
# Absent in a job made before the core sent the sender (compatibility), and when the sender has no stored name.
_OPTIONAL_FIELDS = frozenset({"sender"})
_CARD_FIELDS = frozenset({"card_id", "lemma"})
_SENDER_FIELDS = frozenset({"first_name", "username"})

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
# WORKING TEXT (not set by the specification): the label of the sender line (answer Q20,
# 4.5а-Н2); the old bot has none; final wording waits for the owner.
SENDER_LABELS: dict[Language, str] = {
    "ru": "От:",
    "uk": "Від:",
    "en": "From:",
}
# WORKING TEXT (not set by the specification): the label of the full inbox number (answer
# Q20, 4.5а-Н2); the old bot has none; final wording waits for the owner.
NUMBER_LABELS: dict[Language, str] = {
    "ru": "Номер обращения:",
    "uk": "Номер звернення:",
    "en": "Message number:",
}


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise RenderError(f"The inbox_message {name} must be a non-empty string")
    return value


def _sender_line(sender: Any, language: Language) -> str | None:
    """The sender line, e.g. "From: Anna (@anna_k)"; None when the fact names nobody."""
    if not isinstance(sender, Mapping) or not set(sender) <= _SENDER_FIELDS:
        raise RenderError("The inbox_message sender has unexpected facts")
    if not all(type(value) is str for value in sender.values()):
        raise RenderError("The inbox_message sender facts must be strings")
    # A blank value names nobody; a message must not be lost over a name it cannot show.
    name = sender.get("first_name", "")
    username = sender.get("username", "")
    handle = f"@{username}" if username.strip() else ""
    if name.strip():
        shown = f"{name} ({handle})" if handle else name
    else:
        shown = handle
    return f"{SENDER_LABELS[language]} {shown}" if shown else None


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    if not _FIELDS <= set(body) <= _FIELDS | _OPTIONAL_FIELDS:
        raise RenderError("The inbox_message body has unexpected facts")
    category, text, card = body["category"], _text(body["text"], "text"), body["card"]
    inbox_id = _text(body["inbox_id"], "inbox_id")
    sender = _sender_line(body["sender"], language) if "sender" in body else None
    if category == COMPLAINT:
        if not isinstance(card, Mapping) or set(card) != _CARD_FIELDS:
            raise RenderError("A complaint names the card")
        card_id = _text(card["card_id"], "card_id")
        heading = HEADINGS[COMPLAINT][language].format(lemma=_text(card["lemma"], "lemma"))
    elif category == FEEDBACK:
        if card is not None:
            raise RenderError("A message without a complaint names no card")
        card_id = None
        heading = HEADINGS[FEEDBACK][language]
    else:
        raise RenderError("The inbox_message category is unknown")
    number = f"{NUMBER_LABELS[language]} {inbox_id}"
    lines = [heading, number] if sender is None else [heading, sender, number]
    return OutgoingMessage("\n".join(lines) + f"\n\n{text}", app_card=card_id)


TEMPLATE = Template(KIND, render, examples=(
    {"inbox_id": "5d1a7c3e-0000-4000-8000-000000000001", "category": COMPLAINT,
     "text": "The plural is wrong.", "card": {"card_id": "5d1a7c3e-0000-4000-8000-000000000002", "lemma": "Haus"}},
    {"inbox_id": "5d1a7c3e-0000-4000-8000-000000000003", "category": FEEDBACK,
     "text": "Thank you for the app.", "card": None},
    {"inbox_id": "5d1a7c3e-0000-4000-8000-000000000004", "category": COMPLAINT,
     "text": "The plural is wrong.", "card": {"card_id": "5d1a7c3e-0000-4000-8000-000000000002", "lemma": "Haus"},
     "sender": {"first_name": "Anna", "username": "anna_k"}},
    {"inbox_id": "5d1a7c3e-0000-4000-8000-000000000005", "category": FEEDBACK,
     "text": "Thank you for the app.", "card": None, "sender": {"first_name": "Anna"}},
))
