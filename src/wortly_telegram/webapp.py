"""The Mini App address, the button that opens it and the bot's inline keyboards (design T-D §6, §7.1).

The address is a deployment parameter of the adapter (Q15): it comes from the
configuration only, has no default here, and must be an https URL. It is never a
delivery fact (§4.3). The button is an inline web_app button; a reply-keyboard
web_app button is not used, its data exchange differs (§6 п.1).

A card button (answer Q20) opens the same address with the card id in the query
(``?card=<id>``): Telegram opens a web_app URL as given and puts its launch data in
the fragment, so the query reaches the Mini App and the signed initData is unchanged.
The id is a reference only; the Mini App reads it, signs in as usual and asks the core
for the card, which answers by the user's own rights (Q23).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from .messages import CallbackButton, Language, OutgoingMessage


# WORKING TEXT (not set by the specification): the label of the old bot's start button
# (OLD bot/i18n.py "start"), which the old approval notice refers to; final wording waits
# for the owner (Q20).
BUTTON_LABELS: dict[Language, str] = {
    "ru": "Старт",
    "uk": "Старт",
    "en": "Start",
}


# Wording approved by the owner (S05, 08.10.2026); display conditions follow TA.
CARD_BUTTON_LABELS: dict[Language, str] = {
    "ru": "Открыть карточку",
    "uk": "Відкрити картку",
    "en": "Open the card",
}
# The query parameter the Mini App reads at launch (miniapp src/app/launchCard.ts).
CARD_PARAMETER = "card"


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


def card_address(address: str, card_id: str) -> str:
    """The Mini App address with the card id as the ``card`` query parameter; other parameters are kept."""
    parts = urlsplit(checked_address(address))
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name != CARD_PARAMETER]
    query.append((CARD_PARAMETER, card_id))
    return urlunsplit(parts._replace(query=urlencode(query)))


def card_keyboard(address: str, card_id: str, language: Language) -> InlineKeyboardMarkup:
    """One inline button that opens the Mini App at the given card."""
    button = InlineKeyboardButton(CARD_BUTTON_LABELS[language], web_app=WebAppInfo(card_address(address, card_id)))
    return InlineKeyboardMarkup(((button,),))


def buttons_keyboard(rows: tuple[tuple[CallbackButton, ...], ...]) -> InlineKeyboardMarkup:
    """Inline callback buttons, row by row, as a template or a service function lays them out."""
    return InlineKeyboardMarkup(tuple(
        tuple(InlineKeyboardButton(button.label, callback_data=button.data) for button in row) for row in rows))


def keyboard(message: OutgoingMessage, address: str, language: Language) -> InlineKeyboardMarkup | None:
    """The inline keyboard of a message: the Mini App button, its card button, its callback buttons, or none."""
    if message.app_button:
        return launch_keyboard(address, language)
    if message.app_card is not None:
        return card_keyboard(address, message.app_card, language)
    return buttons_keyboard(message.buttons) if message.buttons else None
