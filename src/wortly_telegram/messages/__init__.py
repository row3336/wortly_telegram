"""Texts the adapter sends, in the recipient's language (design T-D §4).

The core hands over facts, never text (TC:284; jobs/models.py DeliveryFacts). A job's
template turns the facts of the committed snapshot into one OutgoingMessage. The bot's
own replies (/start) are written in the same three languages.

Languages: ru, uk, en (§4.2). A recipient's language is the saved interface language;
someone without a profile is answered in the language of their Telegram client, chosen
by the core's own rule (identity/languages.py, English by default).

Nothing here imports a database driver.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from wortly.identity.languages import initial_language


Language = Literal["ru", "uk", "en"]
LANGUAGES: tuple[Language, ...] = ("ru", "uk", "en")


class RenderError(ValueError):
    """The facts cannot become a message; nothing was sent (pre_send_error, §3.3).

    The message names the fault only, never a fact or a text.
    """


# Telegram's bound of an inline button's callback_data, in bytes (Bot API InlineKeyboardButton).
CALLBACK_DATA_BYTES = 64


@dataclass(frozen=True)
class CallbackButton:
    """An inline button whose press comes back to the bot as callback_data (design §7.1 п.3).

    ``data`` holds object references only, never the actor; a service function with the
    same callback prefix answers the press.
    """

    label: str
    data: str

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label.strip():
            raise RenderError("A button needs a non-empty label")
        if (type(self.data) is not str or not self.data
                or len(self.data.encode("utf-8")) > CALLBACK_DATA_BYTES):
            raise RenderError("A button needs callback data of 1 to 64 bytes")


@dataclass(frozen=True)
class OutgoingMessage:
    """Plain text without parse_mode (ВП-16) and at most one keyboard source.

    The keyboard is the Mini App button, the Mini App button that opens one card
    (``app_card``, answer Q20), or rows of callback buttons. ``app_card`` is a card id
    only: it grants nothing, the Mini App still signs in with its own initData.
    ``app_label`` names the Mini App button when a kind has its own approved label (the
    Learning reminder, S04); the button opens the same address, without it the start label.
    ``silent`` sends the message without a notification sound (Bot API disable_notification);
    only the kind that asks for it is quiet, every other message is sent as before.
    ``parse_mode`` is one of the owner's two narrow exceptions to "no parse_mode" (R9-04,
    R9-05; К19-049): among the jobs only the ``#daily`` report asks for it, to send its text
    as a monospace Markdown block; among the replies only the copyable echo of a common field
    (wortly_telegram.echo). The text then already carries the escapes MarkdownV2 needs
    (messages._markdown_v2); every other message has ``None``.
    """

    text: str = field(repr=False)
    app_button: bool = False
    buttons: tuple[tuple[CallbackButton, ...], ...] = ()
    app_card: str | None = field(default=None, repr=False)
    app_label: str | None = None
    silent: bool = False
    parse_mode: Literal["MarkdownV2"] | None = None

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text.strip():
            raise RenderError("A message needs a non-empty text")
        if type(self.app_button) is not bool:
            raise RenderError("app_button must be a boolean")
        if type(self.silent) is not bool:
            raise RenderError("silent must be a boolean")
        if self.parse_mode is not None and self.parse_mode != "MarkdownV2":
            raise RenderError("parse_mode is MarkdownV2 or absent")
        if not isinstance(self.buttons, tuple) or not all(
                isinstance(row, tuple) and row and all(isinstance(button, CallbackButton) for button in row)
                for row in self.buttons):
            raise RenderError("buttons must be non-empty rows of CallbackButton")
        if self.app_label is not None and (
                not self.app_button or type(self.app_label) is not str or not self.app_label.strip()):
            raise RenderError("app_label names the Mini App button and is a non-empty text")
        if self.app_card is not None and not _canonical_uuid(self.app_card):
            # The Mini App opens only a well-formed card id; anything else would be dropped there.
            raise RenderError("app_card must be a card id")
        if sum((self.app_button, bool(self.buttons), self.app_card is not None)) > 1:
            # No kind needs two; one keyboard source keeps the channel's markup simple.
            raise RenderError("A message has one keyboard source: the Mini App button, a card button or callback buttons")


def _canonical_uuid(value: object) -> bool:
    """True for a UUID in its canonical lower-case form, the form the core hands over."""
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


Render = Callable[[Mapping[str, Any], Language], OutgoingMessage]


@dataclass(frozen=True)
class Template:
    """One job kind's template, declared by its module as ``TEMPLATE``.

    ``render`` gets the envelope's body and a supported language. ``examples`` are
    bodies the contract test renders in every language, so a missing language or a
    template that cannot render its own kind fails the test instead of a delivery.
    """

    kind: str
    render: Render
    examples: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if type(self.kind) is not str or not self.kind.strip() or self.kind != self.kind.strip():
            raise ValueError("A template needs a job kind name without surrounding blanks")
        if not callable(self.render):
            raise ValueError(f"Template {self.kind!r} needs a callable render")
        if not self.examples:
            raise ValueError(f"Template {self.kind!r} needs at least one example body")


def reply_language(saved: str | None, telegram: str | None) -> Language:
    """The language of a reply: the saved interface language, else the Telegram client's."""
    return initial_language(saved=saved, telegram=telegram, browser=None)
