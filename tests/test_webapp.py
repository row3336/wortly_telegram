"""The card button of a complaint (answer Q20, variant A): the Mini App address with the card id in the query."""

from __future__ import annotations

import pytest

from wortly_telegram.messages import OutgoingMessage, RenderError
from wortly_telegram.webapp import CARD_BUTTON_LABELS, WebAppAddressError, card_address, keyboard


CARD_ID = "5d1a7c3e-0000-4000-8000-000000000002"


def test_the_card_address_adds_the_card_parameter_to_the_configured_address():
    assert card_address("https://app.example.test/wortly/", CARD_ID) == f"https://app.example.test/wortly/?card={CARD_ID}"


def test_the_card_address_keeps_other_parameters_and_replaces_a_configured_card():
    address = "https://app.example.test/wortly/?v=2&card=old"
    assert card_address(address, CARD_ID) == f"https://app.example.test/wortly/?v=2&card={CARD_ID}"


def test_the_card_address_checks_the_configured_address():
    with pytest.raises(WebAppAddressError):
        card_address("http://app.example.test/", CARD_ID)


@pytest.mark.parametrize("language", ["ru", "uk", "en"])
def test_a_card_message_has_one_web_app_button_with_its_label(language):
    markup = keyboard(OutgoingMessage("text", app_card=CARD_ID), "https://app.example.test/", language)
    [[button]] = markup.inline_keyboard
    assert button.text == CARD_BUTTON_LABELS[language]
    assert button.web_app.url == f"https://app.example.test/?card={CARD_ID}"
    assert button.callback_data is None and button.url is None


def test_a_message_without_a_card_has_no_card_button():
    assert keyboard(OutgoingMessage("text"), "https://app.example.test/", "en") is None


@pytest.mark.parametrize("card", ["x", "", 5, CARD_ID.upper()])
def test_a_card_button_needs_a_canonical_card_id(card):
    with pytest.raises(RenderError):
        OutgoingMessage("text", app_card=card)


def test_a_card_button_is_the_only_keyboard_source():
    with pytest.raises(RenderError):
        OutgoingMessage("text", app_button=True, app_card=CARD_ID)


def test_the_card_id_is_not_in_the_message_repr():
    assert CARD_ID not in repr(OutgoingMessage("text", app_card=CARD_ID))
