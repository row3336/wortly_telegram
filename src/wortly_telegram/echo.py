"""The copyable echo of a common field in the service bot (Q07, R2-02; R9-05, К19-049; TZC:470).

Q07 keeps the owner's correction of common fields from the bot "with the old convenience of
the echo message and copying by a tap". The old bot sent, before it asked for a new value of
a non-empty field, a heading, the current value as an inline code entity (Telegram copies it
on a tap) and a hint (OLD bot/handlers/clarify_fields.py:105 ``_send_effective_value_echo``;
an empty field had no echo). Wortly keeps that and adds what the owner asked for (R9-05): the
value is a block of its own with an empty line above and below it, because a one-line echo
without the gap is often not copied at once.

This is the second narrow exception to ВП-16 (R9-05, К19-049): the echo is sent with
``parse_mode="MarkdownV2"``. Every part of the text is escaped (messages._markdown_v2): the
heading and the hint as ordinary MarkdownV2 text, the value inside its code entity, so no
value can end the block or add markup. The Mini App has no echo (R9-05).

Connection point (DEV-17 B2, B3): the owner's common editor (service/card_editor.py) calls
``send_echo`` with the field's current value, as the old bot did, before it asks for the new
one: a translation before its ✏️ input, a grammar form before its value buttons. Nothing here
reads or writes a card.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from telegram import Bot, Message

from .messages import Language, OutgoingMessage, RenderError
from .messages._markdown_v2 import escape_pre, escape_text
from .reply import send_reply


# WORKING TEXT (not set by the specification): the old bot's heading and hint of the echo
# (OLD bot/i18n.py "clarify_current_value", "clarify_tap_copy"); final wording waits for
# the owner (T-TEXTS-PAGE).
ECHO_TEXTS: dict[Language, dict[str, str]] = {
    "ru": {"heading": "Текущее значение", "hint": "Нажми на значение, чтобы скопировать."},
    "uk": {"heading": "Поточне значення", "hint": "Торкнись значення, щоб скопіювати."},
    "en": {"heading": "Current value", "hint": "Tap the value to copy it."},
}


def echo_message(value: str, language: Language) -> OutgoingMessage | None:
    """The echo of ``value``: heading, empty line, the value as inline code, empty line, hint.

    None for a blank value: as in the old bot, an empty field has nothing to copy.
    """
    if type(value) is not str:
        raise RenderError("A common field's value is a string")
    if not value.strip():
        return None
    texts = ECHO_TEXTS[language]
    text = f"{escape_text(texts['heading'])}\n\n`{escape_pre(value)}`\n\n{escape_text(texts['hint'])}"
    return OutgoingMessage(text, parse_mode="MarkdownV2")


async def send_echo(bot: Bot, chat_id: int, value: str, language: Language, *,
                    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> Message | None:
    """Send the echo as a reply (ВП-28, reply.send_reply); None when there is nothing to echo."""
    message = echo_message(value, language)
    if message is None:
        return None
    return await send_reply(lambda: bot.send_message(chat_id=chat_id, text=message.text,
                                                     parse_mode=message.parse_mode),
                            sleep=sleep)
