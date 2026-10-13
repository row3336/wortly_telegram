"""The copyable echo of a common field (R9-05, К19-049): MarkdownV2, escaped, empty lines around the value.

A small reader of the MarkdownV2 subset the echo uses (backslash escapes and inline code)
stands in for Telegram's parser: it proves the shown text and that the code entity holds
exactly the value, whatever the value contains. A real client is checked on a test bot later.
"""

from __future__ import annotations

import pytest
from telegram.helpers import escape_markdown

from wortly_telegram.echo import ECHO_TEXTS, echo_message, send_echo
from wortly_telegram.messages import LANGUAGES, RenderError
from wortly_telegram.messages._markdown_v2 import escape_pre, escape_text
from wortly_telegram.messages.registry import discover


# What an owner may have stored in a common field: every MarkdownV2 special, HTML, a link, blanks.
HOSTILE = [
    "Haus", "das Haus, die Häuser", "`", "\\", "``` x ```", "\\`", "a`b\\c", "_*[]()~`>#+-=|{}.!",
    "<b>fett</b> [a](https://example.test)", " leading and trailing ", "zwei\nZeilen", "😀 emoji",
    "*bold*", "||spoiler||", "\\\\`\\",
]
SPECIALS = "\\_*[]()~`>#+-=|{}.!"


def read_markdown_v2(text: str) -> tuple[str, list[tuple[int, int]]]:
    """The shown text and the code spans (start, end in characters) of the echo's MarkdownV2 subset."""
    shown: list[str] = []
    spans: list[tuple[int, int]] = []
    start = None
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 1
            assert index < len(text), "a trailing backslash"
            escaped = text[index]
            assert escaped in SPECIALS if start is None else escaped in "\\`", f"needless escape {escaped!r}"
            shown.append(escaped)
        elif char == "`":
            if start is None:
                start = len(shown)
            else:
                spans.append((start, len(shown)))
                start = None
        else:
            assert start is not None or char not in SPECIALS, f"unescaped {char!r} outside the code"
            shown.append(char)
        index += 1
    assert start is None, "an unclosed code entity"
    return "".join(shown), spans


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("value", HOSTILE)
def test_the_echo_shows_heading_value_and_hint_with_empty_lines_around_the_value(language, value):
    message = echo_message(value, language)
    shown, spans = read_markdown_v2(message.text)
    texts = ECHO_TEXTS[language]
    assert shown == f"{texts['heading']}\n\n{value}\n\n{texts['hint']}"
    start = len(texts["heading"]) + 2
    # Exactly one code entity, and it is the value: nothing a value holds can end it early.
    assert spans == [(start, start + len(value))]


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_exact_text_of_a_plain_value(language):
    texts = ECHO_TEXTS[language]
    assert echo_message("das Haus", language).text == (
        f"{escape_markdown(texts['heading'], version=2)}\n\n`das Haus`\n\n{escape_markdown(texts['hint'], version=2)}")


def test_the_english_echo_as_it_goes_out():
    assert echo_message("a`b", "en").text == "Current value\n\n`a\\`b`\n\nTap the value to copy it\\."


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_echo_is_markdownv2_ordinary_and_without_buttons(language):
    message = echo_message("Haus", language)
    assert message.parse_mode == "MarkdownV2"
    assert message.silent is False
    assert (message.app_button, message.buttons, message.app_card) == (False, (), None)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("value", ["", " ", "\n\t "])
def test_a_blank_field_has_no_echo(language, value):
    # As in the old bot (clarify_fields.py:105): an empty field has nothing to copy.
    assert echo_message(value, language) is None


@pytest.mark.parametrize("value", [None, 5, b"Haus", ["Haus"]])
def test_a_value_that_is_no_string_fails_before_any_network_call(value):
    with pytest.raises(RenderError):
        echo_message(value, "en")


@pytest.mark.parametrize("text", HOSTILE + [SPECIALS, "".join(chr(code) for code in range(32, 127))])
def test_the_escapes_match_ptbs_own_markdownv2_escapes(text):
    assert escape_text(text) == escape_markdown(text, version=2)
    assert escape_pre(text) == escape_markdown(text, version=2, entity_type="code")
    assert escape_pre(text) == escape_markdown(text, version=2, entity_type="pre")


def test_the_echo_is_no_job_template():
    # A reply, not a delivery: no kind in the registry, so no delivery can ask for it.
    assert not any("echo" in kind for kind in discover().kinds)


# -- on the wire, through PTB and the fake Telegram ---------------------------------------------

@pytest.mark.parametrize("language", LANGUAGES)
async def test_send_echo_sends_one_markdownv2_reply(telegram, sleeps, language):
    async with telegram.bot() as bot:
        await send_echo(bot, 5005, "a`b\\c", language, sleep=sleeps)
    [call] = telegram.calls_to("sendMessage")
    assert call.params["chat_id"] == "5005"
    assert call.params["parse_mode"] == "MarkdownV2"
    assert call.params["text"] == echo_message("a`b\\c", language).text
    assert "disable_notification" not in call.params and "reply_markup" not in call.params


async def test_send_echo_sends_nothing_for_a_blank_field(telegram, sleeps):
    async with telegram.bot() as bot:
        assert await send_echo(bot, 5005, "  ", "ru", sleep=sleeps) is None
    assert telegram.calls_to("sendMessage") == []


async def test_send_echo_repeats_as_every_reply_does(telegram, sleeps):
    telegram.fail("sendMessage", 429, "Too Many Requests", retry_after=2)
    async with telegram.bot() as bot:
        await send_echo(bot, 5005, "Haus", "en", sleep=sleeps)
    assert len(telegram.calls_to("sendMessage")) == 2
    assert sleeps.pauses == [3]
