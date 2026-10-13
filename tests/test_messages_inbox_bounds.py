"""R7-01 (К19-042): the bounds of the service part of an inbox_message and Telegram's 4096.

Only a value over its bound changes: the lemma keeps 40 UTF-16 units and gets "…", the first
name keeps 128, the username 32; the user's text (at most 3700, checked by the Mini App and
the core) is never cut. A cut never splits a surrogate pair.
"""

from __future__ import annotations

import pytest

from wortly_telegram.messages import LANGUAGES
from wortly_telegram.messages.inbox_message import (
    LEMMA_CUT_MARK, LEMMA_MAX_UTF16, NAME_MAX_UTF16, TEMPLATE, TEXT_MAX_UTF16, USERNAME_MAX_UTF16, bounded,
    utf16_units,
)


INBOX_ID = "5d1a7c3e-0000-4000-8000-000000000001"
CARD_ID = "5d1a7c3e-0000-4000-8000-000000000002"
# Telegram's bound of a message text (Bot API sendMessage), in UTF-16 units; checked on a test bot later (R7-01).
TELEGRAM_TEXT_MAX = 4096
EMOJI = "😀"  # U+1F600: two UTF-16 units


def complaint(lemma: str, text: str = "x", sender: dict | None = None) -> dict:
    body = {"inbox_id": INBOX_ID, "category": "complaint", "text": text, "card": {"card_id": CARD_ID, "lemma": lemma}}
    return body if sender is None else {**body, "sender": sender}


def feedback(text: str = "x", sender: dict | None = None) -> dict:
    body = {"inbox_id": INBOX_ID, "category": "feedback", "text": text, "card": None}
    return body if sender is None else {**body, "sender": sender}


def heading_lemma(text: str, language: str) -> str:
    """The lemma as shown between the heading's quotes."""
    heading = text.split("\n", 1)[0]
    opening, closing = ("“", "”") if language == "en" else ("«", "»")
    return heading[heading.index(opening) + 1:heading.rindex(closing)]


def test_the_bounds_are_the_owners_numbers():
    assert (LEMMA_MAX_UTF16, NAME_MAX_UTF16, USERNAME_MAX_UTF16, TEXT_MAX_UTF16) == (40, 128, 32, 3700)
    assert LEMMA_CUT_MARK == "…" and utf16_units(LEMMA_CUT_MARK) == 1


@pytest.mark.parametrize(("value", "units"), [("", 0), ("Haus", 4), ("ы" * 3, 3), (EMOJI, 2), ("⚠️", 2),
                                                ("a" + EMOJI + "b", 4), ("\ud800", 1)])
def test_utf16_units_count_as_telegram_does(value, units):
    assert utf16_units(value) == units


@pytest.mark.parametrize(("value", "limit", "mark", "expected"), [
    ("a" * 40, 40, "…", "a" * 40),
    ("a" * 41, 40, "…", "a" * 40 + "…"),
    ("a" * 38 + EMOJI, 40, "…", "a" * 38 + EMOJI),
    ("a" * 39 + EMOJI, 40, "…", "a" * 39 + "…"),
    (EMOJI * 21, 40, "…", EMOJI * 20 + "…"),
    ("a" + EMOJI * 20, 40, "…", "a" + EMOJI * 19 + "…"),
    ("a" * 129, 128, "", "a" * 128),
    ("", 32, "", ""),
])
def test_bounded_keeps_a_short_value_and_never_splits_a_surrogate_pair(value, limit, mark, expected):
    result = bounded(value, limit, mark)
    assert result == expected
    if result != value:
        assert utf16_units(result.removesuffix(mark)) <= limit
        # A character above U+FFFF is kept whole or dropped whole, never as a lone surrogate.
        assert all(not "\ud800" <= char <= "\udfff" for char in result)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("lemma", ["Haus", "a" * 40, "ä" * 40, EMOJI * 20, "a" * 38 + EMOJI])
def test_a_lemma_within_forty_units_is_unchanged(language, lemma):
    assert heading_lemma(TEMPLATE.render(complaint(lemma), language).text, language) == lemma


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(("lemma", "shown"), [
    ("a" * 41, "a" * 40 + "…"),
    ("Donaudampfschifffahrtsgesellschaftskapitänsmütze", "Donaudampfschifffahrtsgesellschaftskapit…"),
    ("a" * 39 + EMOJI, "a" * 39 + "…"),
    (EMOJI * 21, EMOJI * 20 + "…"),
])
def test_a_longer_lemma_keeps_forty_units_and_gets_the_mark(language, lemma, shown):
    assert heading_lemma(TEMPLATE.render(complaint(lemma), language).text, language) == shown


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_name_and_a_username_over_their_bounds_are_cut_without_a_mark(language):
    sender = {"first_name": "N" * 127 + EMOJI, "username": "u" * 33}
    line = TEMPLATE.render(feedback(sender=sender), language).text.split("\n")[1]
    assert line.endswith(f" {'N' * 127} (@{'u' * 32})")


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_name_and_a_username_at_their_bounds_are_unchanged(language):
    sender = {"first_name": "N" * 126 + EMOJI, "username": "u" * 32}
    line = TEMPLATE.render(feedback(sender=sender), language).text.split("\n")[1]
    assert line.endswith(f" {'N' * 126}{EMOJI} (@{'u' * 32})")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("text", ["ы" * TEXT_MAX_UTF16, EMOJI * (TEXT_MAX_UTF16 // 2), "ы" * 5000])
def test_the_users_text_is_never_cut(language, text):
    assert TEMPLATE.render(complaint("a" * 100, text), language).text.endswith("\n\n" + text)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_longest_complaint_fits_telegrams_bound(language):
    # The worst case R7-01 calculated: every bounded value over its bound, a text of 3700 units.
    sender = {"first_name": EMOJI * 200, "username": "u" * 200}
    text = "ы" * TEXT_MAX_UTF16
    message = TEMPLATE.render(complaint(EMOJI * 100, text, sender), language).text
    service = utf16_units(message) - utf16_units(text)
    assert utf16_units(message) <= TELEGRAM_TEXT_MAX
    # The owner's calculation: a service part of 297 units at most, a margin of 99 (R7-01).
    assert service <= 297, service


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_longest_feedback_fits_telegrams_bound(language):
    sender = {"first_name": "N" * 500, "username": "u" * 500}
    text = "ы" * TEXT_MAX_UTF16
    assert utf16_units(TEMPLATE.render(feedback(text, sender), language).text) <= TELEGRAM_TEXT_MAX
