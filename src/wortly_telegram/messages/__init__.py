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

from wortly.identity.languages import initial_language


Language = Literal["ru", "uk", "en"]
LANGUAGES: tuple[Language, ...] = ("ru", "uk", "en")


class RenderError(ValueError):
    """The facts cannot become a message; nothing was sent (pre_send_error, §3.3).

    The message names the fault only, never a fact or a text.
    """


@dataclass(frozen=True)
class OutgoingMessage:
    """Plain text without parse_mode (ВП-16) and, optionally, the Mini App button."""

    text: str = field(repr=False)
    app_button: bool = False

    def __post_init__(self) -> None:
        if type(self.text) is not str or not self.text.strip():
            raise RenderError("A message needs a non-empty text")
        if type(self.app_button) is not bool:
            raise RenderError("app_button must be a boolean")


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
