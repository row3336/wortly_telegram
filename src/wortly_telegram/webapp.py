"""The Mini App address and the button that opens it (design T-D §6).

The address is a deployment parameter of the adapter (Q15): it comes from the
configuration only, has no default here, and must be an https URL. It is never a
delivery fact (§4.3). The button is an inline web_app button; a reply-keyboard
web_app button is not used, its data exchange differs (§6 п.1).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from .messages import Language


# WORKING TEXT (not set by the specification): the label of the old bot's start button
# (OLD bot/i18n.py "start"), which the old approval notice refers to; final wording waits
# for the owner (Q20).
BUTTON_LABELS: dict[Language, str] = {
    "ru": "Старт",
    "uk": "Старт",
    "en": "Start",
}


class WebAppAddressError(ValueError):
    """The configured Mini App address is not an https URL; the message carries no value."""


def checked_address(value: object) -> str:
    """Return ``value`` if it is an absolute https URL with a host and nothing hidden in it."""
    if type(value) is not str or not value or any(character.isspace() for character in value):
        raise WebAppAddressError("The Mini App address must be a URL without blanks")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        # Credentials in the address would be sent to every user who gets the button.
        credentials = parts.username is not None or parts.password is not None
    except ValueError:
        raise WebAppAddressError("The Mini App address is not a valid URL") from None
    if parts.scheme != "https" or not host:
        raise WebAppAddressError("The Mini App address must be an https URL with a host")
    if credentials:
        raise WebAppAddressError("The Mini App address must not carry credentials")
    return value


def launch_keyboard(address: str, language: Language) -> InlineKeyboardMarkup:
    """One inline button that opens the Mini App at the configured address."""
    button = InlineKeyboardButton(BUTTON_LABELS[language], web_app=WebAppInfo(checked_address(address)))
    return InlineKeyboardMarkup(((button,),))
