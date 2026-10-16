"""The weekly_report template on the facts of the core's future builder (Q05; К12-008…012; DEV-17).

The texts are a working copy until the owner approves them (G05); these tests pin the shape
and the numbers, not the final wording. Local fake transport only; no database, no network.
"""

from __future__ import annotations

import json
import logging

import pytest

from conftest import TELEGRAM_SETTINGS
from fakes.jobs import envelope, snapshot
from fakes.telegram_api import WEB_APP_URL
from wortly.jobs.channel import SENT
from wortly_telegram.app import make_bot
from wortly_telegram.delivery.channel import TelegramChannel
from wortly_telegram.delivery.main import loop_runner
from wortly_telegram.messages import LANGUAGES, RenderError
from wortly_telegram.messages.registry import discover
from wortly_telegram.messages.weekly_report import KIND, TEMPLATE, WEEKLY_TEXTS, share_tenths


def body(*, week_start="2026-10-05", study_actions=True, cards=12, correct=9):
    return {"week_start": week_start, "study_actions": study_actions,
            "written_cards": cards, "written_correct": correct}


def facts(language="ru", fact_body=None, user_id="4242"):
    return json.dumps(envelope(KIND, language=language, telegram_user_id=user_id,
                               body=body() if fact_body is None else fact_body))


def render(language="en", **changes):
    return discover().prepare(KIND, facts(language, body(**changes))).message


EXPECTED = {
    "ru": "Недельный отчёт: 2026-10-05 – 2026-10-11\nКарточек в письменных квизах: 12\nВерно: 9\nНеверно: 3\n"
          "Доля верных: 75,0%\nСовет: уменьшить число новых слов.",
    "uk": "Тижневий звіт: 2026-10-05 – 2026-10-11\nКарток у письмових квізах: 12\nВірно: 9\nНевірно: 3\n"
          "Частка вірних: 75,0%\nПорада: зменшити кількість нових слів.",
    "en": "Weekly report: 2026-10-05 – 2026-10-11\nWritten quiz cards: 12\nCorrect: 9\nWrong: 3\n"
          "Share correct: 75.0%\nAdvice: reduce the number of new words.",
}
ZERO = {
    "ru": "Недельный отчёт: 2026-10-05 – 2026-10-11\nКарточек в письменных квизах: 0\n"
          "На этой неделе были учебные действия, но письменных ответов не было.",
    "uk": "Тижневий звіт: 2026-10-05 – 2026-10-11\nКарток у письмових квізах: 0\n"
          "Цього тижня були навчальні дії, але письмових відповідей не було.",
    "en": "Weekly report: 2026-10-05 – 2026-10-11\nWritten quiz cards: 0\n"
          "There were study actions this week, but no written answers.",
}


def test_the_kind_is_registered_with_the_template():
    assert discover().template(KIND) is TEMPLATE
    assert KIND == "weekly_report"


@pytest.mark.parametrize("language", LANGUAGES)
def test_a_week_with_written_cards_is_the_counts_the_share_and_the_advice(language):
    message = discover().prepare(KIND, facts(language)).message
    assert message.text == EXPECTED[language]


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_report_is_quiet_and_has_no_keyboard(language):
    message = discover().prepare(KIND, facts(language)).message
    assert message.silent is True
    assert message.app_button is False and message.buttons == () and message.app_card is None


@pytest.mark.parametrize("language", LANGUAGES)
def test_study_actions_without_written_answers_say_zero_cards_and_no_percent(language):
    message = discover().prepare(KIND, facts(language, body(cards=0, correct=0))).message
    assert message.text == ZERO[language]
    assert message.silent is True
    assert "%" not in message.text


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_example_of_the_template_renders_in_every_language(language):
    for example in TEMPLATE.examples:
        assert TEMPLATE.render(example, language).silent is True


@pytest.mark.parametrize(("cards", "correct", "shown", "advice"), [
    (10, 7, "70.0", "reduce"),
    (1000, 799, "79.9", "reduce"),
    (10000, 7996, "80.0", "hold"),    # as shown: 79,96 is printed 80,0 and advised like 80,0
    (10, 8, "80.0", "hold"),
    (10, 9, "90.0", "hold"),
    (1000, 901, "90.1", "raise"),
    (3, 2, "66.7", "reduce"),
    (8, 7, "87.5", "hold"),
    (8, 1, "12.5", "reduce"),
    (2000, 1, "0.1", "reduce"),         # 0,05 rounds half up to 0,1
    (5, 5, "100.0", "raise"),
    (5, 0, "0.0", "reduce"),
])
def test_share_rounding_and_advice_thresholds(cards, correct, shown, advice):
    text = render("en", cards=cards, correct=correct).text
    assert f"Share correct: {shown}%" in text
    assert WEEKLY_TEXTS["en"][advice] in text
    assert {WEEKLY_TEXTS["en"][name] for name in ("reduce", "hold", "raise")} & set(text.split("\n")) \
        == {WEEKLY_TEXTS["en"][advice]}
    assert f"Wrong: {cards - correct}" in text


def test_the_share_is_exact_integer_arithmetic():
    assert share_tenths(1, 3) == 333 and share_tenths(2, 3) == 667 and share_tenths(1, 8) == 125
    assert share_tenths(1, 2000) == 1 and share_tenths(1, 2001) == 0


def test_the_period_is_seven_local_days_from_the_monday_across_a_month_and_a_year():
    assert "2026-12-28 – 2027-01-03" in render(week_start="2026-12-28").text
    assert "2028-02-28 – 2028-03-05" in render(week_start="2028-02-28").text


@pytest.mark.parametrize("fact", [
    body(study_actions=False, cards=0, correct=0),
    body(study_actions=False),
], ids=["empty-week", "cards-without-actions"])
def test_a_week_without_study_actions_is_never_a_message(fact):
    for language in LANGUAGES:
        with pytest.raises(RenderError):
            discover().prepare(KIND, facts(language, fact))


@pytest.mark.parametrize("fact", [
    {},
    {**body(), "extra": 1},
    {key: value for key, value in body().items() if key != "written_correct"},
    {key: value for key, value in body().items() if key != "week_start"},
    body(cards=3, correct=4),
    body(cards=0, correct=1),
    body(cards=-1, correct=-1),
    body(cards=5, correct=-1),
    body(cards=True, correct=1),
    body(cards=5, correct=True),
    body(cards=5.0, correct=3),
    body(cards="5", correct=3),
    body(cards=None, correct=0),
    body(study_actions=1),
    body(study_actions="true"),
    body(study_actions=None),
    body(week_start="2026-10-06"),
    body(week_start="2026-10-32"),
    body(week_start="2026-02-30"),
    body(week_start="20261005"),
    body(week_start="2026-10-05T00:00:00"),
    body(week_start=" 2026-10-05"),
    body(week_start="٢٠٢٦-١٠-٠٥"),
    body(week_start=20261005),
    body(week_start=None),
], ids=["empty", "extra-fact", "no-correct", "no-week", "correct-over-cards", "correct-without-cards",
        "negative-both", "negative-correct", "bool-cards", "bool-correct", "float-cards", "str-cards",
        "none-cards", "actions-int", "actions-str", "actions-none", "not-monday", "bad-day", "bad-date",
        "compact-date", "datetime", "blank-padded", "other-digits", "date-int", "date-none"])
def test_wrong_facts_fail_before_the_network_without_echoing_them(fact):
    for language in LANGUAGES:
        with pytest.raises(RenderError) as raised:
            discover().prepare(KIND, facts(language, fact))
        assert "2026" not in str(raised.value)


RENAMED = {**{key: value for key, value in body().items() if key != "written_cards"}, "written_total": 12}


@pytest.mark.parametrize(("fact", "category"), [
    ({}, "body_keys"),
    ({**body(), "extra": 1}, "body_keys"),
    ({**body(), "pair": "ru-de"}, "body_keys"),
    ({key: value for key, value in body().items() if key != "written_correct"}, "body_keys"),
    ({key: value for key, value in body().items() if key != "week_start"}, "body_keys"),
    (RENAMED, "body_keys"),
    # Faults of the values keep the general category: only the set of keys is body_keys.
    (body(cards=3, correct=4), "render_invalid"),
    (body(week_start="2026-10-06"), "render_invalid"),
    (body(study_actions=False, cards=0, correct=0), "render_invalid"),
], ids=["empty", "extra-fact", "extra-pair", "no-correct", "no-week", "renamed", "correct-over-cards",
        "not-monday", "empty-week"])
def test_a_body_with_other_keys_is_the_category_body_keys_as_in_daily(fact, category):
    for language in LANGUAGES:
        with pytest.raises(RenderError) as raised:
            discover().prepare(KIND, facts(language, fact))
        assert raised.value.reason == category
    if category == "body_keys":
        with pytest.raises(RenderError) as raised:
            TEMPLATE.render(fact, "ru")  # the template itself, not only the registry
        assert raised.value.reason == "body_keys"


def test_another_schema_keeps_its_category():
    for schema in (2, 0):
        raw = envelope(KIND, language="ru", telegram_user_id="4242", body=body(), schema=schema)
        with pytest.raises(RenderError) as raised:
            discover().prepare(KIND, json.dumps(raw))
        assert raised.value.reason == "schema_mismatch"


def test_a_non_mapping_body_or_other_language_is_refused():
    with pytest.raises(RenderError):
        discover().prepare(KIND, facts(fact_body=[]))
    with pytest.raises(RenderError):
        discover().prepare(KIND, facts("de"))
    with pytest.raises(RenderError):
        discover().prepare(KIND, facts(fact_body="week"))


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_text_holds_only_counts_and_dates_not_personal_data(language):
    prepared = discover().prepare(KIND, facts(language, user_id="987654321"))
    text = prepared.message.text
    assert prepared.chat_id == 987654321
    assert "987654321" not in text
    assert "<" not in text and ">" not in text and "@" not in text and "http" not in text
    assert "987654321" not in repr(prepared.message) and text not in repr(prepared.message)
    assert prepared.chat_id not in (prepared.message.app_card, prepared.message.app_label)


def test_the_texts_exist_for_every_language_with_the_same_lines():
    assert set(WEEKLY_TEXTS) == set(LANGUAGES)
    assert len({frozenset(texts) for texts in WEEKLY_TEXTS.values()}) == 1
    assert all(text.strip() for texts in WEEKLY_TEXTS.values() for text in texts.values())


def test_the_text_is_a_working_copy_that_the_owner_has_not_approved():
    from pathlib import Path

    source = Path(__import__("wortly_telegram.messages.weekly_report", fromlist=["x"]).__file__).read_text("utf-8")
    assert source.count("# WORKING TEXT (not set by the specification)") == 1
    assert "Wording approved by the owner" not in source


@pytest.fixture
def channel(telegram, bot_loop):
    run = loop_runner(bot_loop)
    bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
    run(bot.initialize())

    class IdleCapacity:
        def pause(self, duration) -> None:
            pass

    yield TelegramChannel(bot, templates=discover(), web_app_url=WEB_APP_URL, capacity=IdleCapacity(), run=run)
    run(bot.shutdown())


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_report_goes_out_quietly_through_the_fake_transport(telegram, channel, language):
    result = channel.deliver(snapshot(KIND, json.loads(facts(language, user_id="777000"))))
    assert result is SENT
    [call] = telegram.calls_to("sendMessage")
    assert call.params["disable_notification"] == "true"
    assert call.params["chat_id"] == "777000" and call.params["text"] == EXPECTED[language]
    assert "parse_mode" not in call.params and "reply_markup" not in call.params


def test_an_empty_week_reaches_no_request(telegram, channel):
    fact = body(study_actions=False, cards=0, correct=0)
    result = channel.deliver(snapshot(KIND, json.loads(facts(fact_body=fact))))
    assert (result.outcome, result.reason) == ("known_failed", "pre_send_error")
    assert telegram.calls_to("sendMessage") == []


@pytest.mark.parametrize(("raw", "category"), [
    (envelope(KIND, language="ru", telegram_user_id="777000", body={**body(), "extra": 1}), "body_keys"),
    (envelope(KIND, language="ru", telegram_user_id="777000",
              body={key: value for key, value in body().items() if key != "written_correct"}), "body_keys"),
    (envelope(KIND, language="ru", telegram_user_id="777000", body=body(), schema=2), "schema_mismatch"),
    (envelope(KIND, language="ru", telegram_user_id="777000", body=body(study_actions=False, cards=0, correct=0)),
     "render_invalid"),
], ids=["extra-key", "missing-key", "other-schema", "empty-week"])
def test_the_log_of_a_refused_weekly_envelope_tells_the_category_and_no_fact(telegram, channel, caplog, raw, category):
    caplog.set_level(logging.WARNING)
    result = channel.deliver(snapshot(KIND, raw))
    assert (result.outcome, result.reason) == ("known_failed", "pre_send_error")
    [line] = [record.getMessage() for record in caplog.records if "not formatted" in record.getMessage()]
    assert line == f"delivery not formatted: RenderError: {category}"
    assert telegram.calls_to("sendMessage") == []


def test_other_kinds_stay_loud_after_the_registration(telegram, channel):
    channel.deliver(snapshot(facts=envelope(telegram_user_id="777000")))
    [call] = telegram.calls_to("sendMessage")
    assert "disable_notification" not in call.params
