"""The daily_report template: the old ``#daily`` as a monospace MarkdownV2 block (Q06; CON:315; TZC:470; S-C06).

Local fake transport only (PTB's real client behind httpx.MockTransport); no network, no database.
The facts here are synthetic. The core's builder, window and dedup are not under test (DEV-15).
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import re

import pytest
from telegram.helpers import escape_markdown

from conftest import TELEGRAM_SETTINGS
from fakes.jobs import envelope, snapshot
from fakes.telegram_api import WEB_APP_URL
from wortly.jobs.channel import SENT
from wortly_telegram.app import make_bot
from wortly_telegram.delivery.channel import TelegramChannel
from wortly_telegram.delivery.main import loop_runner
from wortly_telegram.messages import LANGUAGES, OutgoingMessage, RenderError
from wortly_telegram.messages import daily_report
from wortly_telegram.messages.daily_report import KIND, TEMPLATE, escape_pre, rate_of, render, zone_of
from wortly_telegram.messages.registry import discover


def body(**changes):
    facts = {"local_date": "2026-10-12", "total": 20, "correct": 17}
    facts.update(changes)
    return facts


def without(key):
    facts = body()
    del facts[key]
    return facts


# Written out by hand: the old bot's i18n (report_retention_daily, report_zone_*); the title has the local
# date without "UTC" (table 6 of project_notifications ed.4, T1).
TITLE = {"ru": "Ретеншн ({day}, первое касание): {rate}%",
         "uk": "Ретеншн ({day}, перший дотик): {rate}%",
         "en": "Retention ({day}, first touch): {rate}%"}
CORRECT = {"ru": "Верно: {c}/{t}", "uk": "Вірно: {c}/{t}", "en": "Correct: {c}/{t}"}
ZONE = {
    "overload": {"ru": "<70%: перегрузка — слишком трудно, сбавь",
                 "uk": "<70%: перевантаження — надто важко, збав темп",
                 "en": "<70%: overload — too hard, ease off"},
    "below_optimal": {"ru": "70-80%: ниже оптимума — трудновато",
                      "uk": "70-80%: нижче оптимуму — важкувато",
                      "en": "70-80%: below optimal — a bit hard"},
    "optimal": {"ru": "80-90%: оптимально", "uk": "80-90%: оптимально", "en": "80-90%: optimal"},
    "slightly_easy": {"ru": "90-95%: легковато — можно добавить слов",
                      "uk": "90-95%: трохи легко — можна додати слів",
                      "en": "90-95%: slightly easy — you can add words"},
    "too_easy": {"ru": ">95%: слишком легко — добавь новых слов",
                 "uk": ">95%: надто легко — додай нових слів",
                 "en": ">95%: too easy — add more new words"},
}


def lines(language, *, day="2026-10-12", rate, correct, total, zone):
    return ["#daily", TITLE[language].format(day=day, rate=rate), CORRECT[language].format(c=correct, t=total),
            ZONE[zone][language]]


def block(plain_lines):
    return "```text\n" + "\n".join(plain_lines) + "\n```"


def unwrap(text):
    """Read a sent text the way Telegram's MarkdownV2 reads a pre block; fail if it would not be one block."""
    match = re.fullmatch(r"```text\n(.*)\n```", text, re.DOTALL)
    assert match, "the text is not exactly one pre block"
    inner = match.group(1)
    # Inside a pre block every "`" and "\" must come escaped by a "\"; nothing else may stay bare.
    assert not re.search(r"`|\\", re.sub(r"\\[\\`]", "", inner)), "an unescaped special character"
    return re.sub(r"\\([\\`])", r"\1", inner)


# -- the text and the block -----------------------------------------------------------------------------

@pytest.mark.parametrize("language", LANGUAGES)
def test_a_valid_day_is_one_monospace_block_in_each_language(language):
    message = render(body(), language)
    plain = lines(language, rate="85.0", correct=17, total=20, zone="optimal")
    assert message.text == block(plain)
    assert unwrap(message.text).split("\n") == plain
    assert message.parse_mode == "MarkdownV2"


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_message_is_ordinary_and_plain_apart_from_the_block(language):
    message = render(body(), language)
    assert message.silent is False  # CON:315: no disable_notification
    assert (message.app_button, message.buttons, message.app_card, message.app_label) == (False, (), None, None)


def test_the_local_date_is_shown_on_the_second_line_after_the_hashtag_without_utc():
    first, second, *_ = unwrap(render(body(local_date="2026-03-01"), "en").text).split("\n")
    assert first == "#daily" and "(2026-03-01, first touch)" in second and "UTC" not in second


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_text_of_the_report_names_utc(language):
    assert not any("UTC" in text for text in daily_report.DAILY_TEXTS[language].values())
    assert "UTC" not in render(body(), language).text


@pytest.mark.parametrize("facts", [
    {"utc_date": "2026-10-12", "total": 20, "correct": 17},
    {"utc_date": "2026-10-12", "local_date": "2026-10-12", "total": 20, "correct": 17},
], ids=["utc-body-of-the-earlier-core", "both-dates"])
def test_the_body_of_the_earlier_core_with_utc_date_is_refused(facts):
    # N-AC33, N-AC41: a core with utc_date and a template with local_date are not released together;
    # the old body is refused before any network call (pre_send_error), never shown as a local day.
    with pytest.raises(RenderError) as error:
        render(facts, "en")
    assert "2026" not in str(error.value)
    with pytest.raises(RenderError):
        discover().prepare(KIND, json.dumps(envelope(KIND, body=facts, schema=2)))


def test_the_key_of_the_body_is_local_date_in_the_examples_and_the_contract():
    assert daily_report._FIELDS == {"local_date", "total", "correct"}
    for example in TEMPLATE.examples:
        assert set(example) == {"local_date", "total", "correct"}


@pytest.mark.parametrize(("correct", "total", "rate", "zone"), [
    (0, 5, "0.0", "overload"),
    (699, 1000, "69.9", "overload"),
    (1749, 2500, "70.0", "below_optimal"),   # 69.96 shows as 70.0 and the zone follows what is shown
    (7, 10, "70.0", "below_optimal"),
    (799, 1000, "79.9", "below_optimal"),
    (4, 5, "80.0", "optimal"),
    (2249, 2500, "90.0", "optimal"),         # 89.96 shows as 90.0
    (9, 10, "90.0", "optimal"),              # 90 is still the optimal zone (80-90 inclusive)
    (901, 1000, "90.1", "slightly_easy"),
    (19, 20, "95.0", "slightly_easy"),       # 95 is still slightly easy (>90-95 inclusive)
    (951, 1000, "95.1", "too_easy"),
    (1, 1, "100.0", "too_easy"),
])
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_percent_and_the_zone_at_the_boundaries(language, correct, total, rate, zone):
    text = render(body(total=total, correct=correct), language).text
    assert unwrap(text).split("\n") == lines(language, rate=rate, correct=correct, total=total, zone=zone)


def test_the_percent_is_the_old_bots_round_to_one_decimal_and_a_tie_follows_it():
    # round(1 / 16 * 100, 1) is 6.2 in Python (the old bot's own call), not the half-up 6.3.
    assert rate_of(1, 16) == 6.2
    assert unwrap(render(body(total=16, correct=1), "en").text).split("\n")[1].endswith(": 6.2%")


def test_the_decimal_mark_is_a_dot_in_every_language_as_in_the_old_bot():
    for language in LANGUAGES:
        assert unwrap(render(body(), language).text).split("\n")[1].endswith(": 85.0%")


def test_the_displayed_percent_is_within_a_twentieth_of_the_exact_share_and_the_zone_matches_it():
    for total in range(1, 61):
        for correct in range(total + 1):
            shown = rate_of(correct, total)
            assert abs(shown - correct * 100 / total) <= 0.05 + 1e-9
            expected = ("overload" if shown < 70 else "below_optimal" if shown < 80 else "optimal" if shown <= 90
                        else "slightly_easy" if shown <= 95 else "too_easy")
            assert zone_of(shown) == expected


def test_every_zone_has_a_line_in_every_language_and_no_other_line_is_used():
    for language in LANGUAGES:
        texts = daily_report.DAILY_TEXTS[language]
        assert set(texts) == {"title", "correct", *ZONE}
        for zone, wording in ZONE.items():
            assert texts[zone] == wording[language]


def test_the_examples_of_the_template_render_in_every_language():
    for example in TEMPLATE.examples:
        for language in LANGUAGES:
            assert unwrap(TEMPLATE.render(example, language).text).startswith("#daily\n")


# -- the escaping -------------------------------------------------------------------------------------

HOSTILE = ["`", "```", "\\", "\\`", "a`b\\c", "end```\n[x](tg://user?id=1)", "_*[]()~>#+-=|{}.!", "😀`😀", "", "plain"]


@pytest.mark.parametrize("text", HOSTILE)
def test_escape_pre_matches_ptbs_own_pre_escape_and_round_trips(text):
    assert escape_pre(text) == escape_markdown(text, version=2, entity_type="pre")
    assert re.sub(r"\\([\\`])", r"\1", escape_pre(text)) == text


def test_a_special_character_in_any_text_line_cannot_end_the_block(monkeypatch):
    for key in ("title", "correct", "optimal"):
        monkeypatch.setitem(daily_report.DAILY_TEXTS["en"], key, daily_report.DAILY_TEXTS["en"][key] + " `\\```")
    message = render(body(), "en")
    plain = unwrap(message.text).split("\n")
    assert plain[1].endswith(" `\\```") and plain[3].endswith(" `\\```")
    assert message.text.startswith("```text\n") and message.text.endswith("\n```")


def test_the_block_has_exactly_one_opening_and_one_closing_fence():
    text = render(body(), "en").text
    assert text.startswith("```text\n") and text.endswith("\n```") and "`" not in text[len("```text\n"):-len("\n```")]


@pytest.mark.parametrize("facts", [
    body(local_date="2026-10-12```\n[x](tg://user?id=1)"),
    body(local_date="2026-10-12`"),
    body(local_date="2026-10-12\\"),
    body(total="5```"),
    body(correct="`1`"),
])
def test_data_with_special_characters_is_refused_not_placed(facts):
    with pytest.raises(RenderError) as error:
        render(facts, "en")
    assert "```" not in str(error.value) and "tg://" not in str(error.value)


# -- zero and missing facts ---------------------------------------------------------------------------

def test_a_day_without_a_first_touch_is_no_message():
    with pytest.raises(RenderError):
        render(body(total=0, correct=0), "en")


@pytest.mark.parametrize("key", ["local_date", "total", "correct"])
def test_a_missing_fact_is_refused(key):
    with pytest.raises(RenderError):
        render(without(key), "en")


@pytest.mark.parametrize("facts", [
    {}, body(extra=1), body(rate=85.0), body(name="Ann"), body(word="Haus"), body(chat_id=777000),
    body(local_date=None), body(local_date=20261012), body(local_date=datetime(2026, 10, 12)),
    body(local_date="2026-10-12T00:00:00"), body(local_date=" 2026-10-12"), body(local_date="2026-10-12\n"),
    body(local_date="2026-02-30"), body(local_date="2026-13-01"), body(local_date="26-10-12"),
    body(local_date="２０２６-１０-１２"),
    body(total=True), body(total=20.0), body(total="20"), body(total=None), body(total=-1),
    body(correct=True), body(correct=17.0), body(correct="17"), body(correct=None), body(correct=-1),
    body(total=5, correct=6),
])
def test_faulty_facts_are_refused_and_the_error_names_no_value(facts):
    with pytest.raises(RenderError) as error:
        render(facts, "en")
    shown = str(error.value)
    assert not any(token in shown for token in ("2026", "Ann", "Haus", "777000", "17", "20"))


# -- no personal data ---------------------------------------------------------------------------------

def test_the_text_holds_only_the_four_lines_of_the_report_and_nothing_personal():
    registry = discover()
    prepared = registry.prepare(KIND, json.dumps(envelope(KIND, language="ru", telegram_user_id="777000123", body=body(), schema=2)))
    assert prepared.chat_id == 777000123 and "777000123" not in prepared.message.text
    assert len(unwrap(prepared.message.text).split("\n")) == 4


def test_the_message_repr_does_not_show_the_text():
    assert "2026" not in repr(render(body(), "en")) and "Retention" not in repr(render(body(), "en"))


# -- the output type and the registry -----------------------------------------------------------------

def test_parse_mode_is_absent_by_default():
    assert OutgoingMessage("text").parse_mode is None


@pytest.mark.parametrize("value", ["Markdown", "HTML", "markdownv2", "", 2, True])
def test_only_markdownv2_is_a_parse_mode(value):
    with pytest.raises(RenderError):
        OutgoingMessage("text", parse_mode=value)


def test_the_kind_is_registered_and_the_only_template_with_a_parse_mode():
    registry = discover()
    assert KIND in registry.kinds and registry.template(KIND) is TEMPLATE
    for kind in registry.kinds - {KIND}:
        template = registry.template(kind)
        for example in template.examples:
            for language in LANGUAGES:
                assert template.render(example, language).parse_mode is None, kind


def test_the_text_is_a_working_copy_that_the_owner_has_not_approved():
    source = Path(daily_report.__file__).read_text("utf-8")
    assert source.count("# WORKING TEXT (not set by the specification)") == 1
    assert "Wording approved by the owner" not in source


# -- through the channel and the fake Telegram transport ------------------------------------------------

class IdleCapacity:
    def pause(self, duration) -> None:
        pass


@pytest.fixture
def channel(telegram, bot_loop):
    run = loop_runner(bot_loop)
    bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
    run(bot.initialize())
    yield TelegramChannel(bot, templates=discover(), web_app_url=WEB_APP_URL, capacity=IdleCapacity(), run=run)
    run(bot.shutdown())


def facts_for(language="ru", user_id="777000", **changes):
    return envelope(KIND, language=language, telegram_user_id=user_id, body=body(**changes), schema=2)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_report_goes_out_as_an_ordinary_markdownv2_block(telegram, channel, language):
    assert channel.deliver(snapshot(KIND, facts_for(language))) is SENT
    [call] = telegram.calls_to("sendMessage")
    assert call.params["parse_mode"] == "MarkdownV2"
    assert call.params["chat_id"] == "777000"
    assert call.params["text"] == block(lines(language, rate="85.0", correct=17, total=20, zone="optimal"))
    assert "disable_notification" not in call.params and "reply_markup" not in call.params
    assert unwrap(call.params["text"]).split("\n")[0] == "#daily"


def test_a_zero_day_reaches_no_request(telegram, channel):
    result = channel.deliver(snapshot(KIND, facts_for(total=0, correct=0)))
    assert (result.outcome, result.reason) == ("known_failed", "pre_send_error")
    assert telegram.calls_to("sendMessage") == []


@pytest.mark.parametrize("changes", [{"local_date": "2026-10-12```"}, {"total": "20"}, {"correct": 21}])
def test_faulty_facts_reach_no_request(telegram, channel, changes):
    result = channel.deliver(snapshot(KIND, facts_for(**changes)))
    assert (result.outcome, result.reason) == ("known_failed", "pre_send_error")
    assert telegram.calls_to("sendMessage") == []


@pytest.mark.parametrize(("raw", "category"), [
    (envelope(KIND, language="ru", telegram_user_id="777000", schema=1,
              body={"utc_date": "2026-10-12", "total": 20, "correct": 17}), "schema_mismatch"),
    (envelope(KIND, language="ru", telegram_user_id="777000", schema=2,
              body={"utc_date": "2026-10-12", "total": 20, "correct": 17}), "body_keys"),
    (envelope("no_such_kind", language="ru", telegram_user_id="777000", schema=2,
              body={"local_date": "2026-10-12", "total": 20, "correct": 17}), "unknown_kind"),
    (envelope(KIND, language="ru", telegram_user_id="777000", schema=2,
              body={"local_date": "2026-10-12", "total": 0, "correct": 0}), "render_invalid"),
], ids=["old-schema", "old-body", "unknown-kind", "empty-day"])
def test_the_log_of_a_refused_envelope_tells_the_category_and_no_fact(telegram, channel, caplog, raw, category):
    caplog.set_level(logging.WARNING)
    snap = snapshot(raw["kind"], raw)
    result = channel.deliver(snap)
    assert (result.outcome, result.reason) == ("known_failed", "pre_send_error")
    [line] = [record.getMessage() for record in caplog.records if "not formatted" in record.getMessage()]
    assert line == f"delivery not formatted: RenderError: {category}"
    assert telegram.calls_to("sendMessage") == []


def test_another_kind_still_goes_out_as_plain_text_and_loud(telegram, channel):
    channel.deliver(snapshot(facts=envelope(telegram_user_id="777000")))
    [call] = telegram.calls_to("sendMessage")
    assert "parse_mode" not in call.params and "disable_notification" not in call.params
