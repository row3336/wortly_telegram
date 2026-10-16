"""The chain builder -> envelope -> renderer against the core's own builders (N-AC33, N-AC41; project_notifications ed.4 §7.3, §7.5).

The core's real ``build`` of a kind (over scripted rows, no database) writes the envelope, and the bot's registry
prepares exactly that text. The pair (kind, schema) of each template is the one its builder writes (ENVELOPE_SCHEMA):
evening_reminder 1, #daily 2 once the core has N4. A core without N3 or N4 skips what it cannot write; with them
every test here runs. weekly_report (schema 1): the core's real build over scripted rows decides the hour, Deferred,
the catch-up and the late switch-on (N4: weekly_report_hour, weekly_report_armed_at), and its body is exactly the four
facts of the template, asserted before the bot renders it. Checked on the core 3bcb16c (ea8e1d9 N3+N5 + N4 0034).
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
import json
from uuid import uuid4

import pytest

from fakes.core_gateway import JOB_EVENT_QUERY, RECIPIENT_QUERY, ScriptedFacts, recipient_row
from wortly.jobs.kinds import discover as discover_kinds
from wortly.jobs.models import Deferred, JobContext
from wortly_telegram.messages import RenderError
from wortly_telegram.messages.registry import discover


UTC = timezone.utc
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
SETTINGS_QUERY = "FROM profile_settings s WHERE s.profile_id"
FACTS_QUERY = "SELECT count(f.card_id) AS total"


def core_module(name: str):
    return import_module(f"wortly.jobs.builders.{name}")


def core_has_n4() -> bool:
    return hasattr(core_module("daily_report"), "date_end")  # the local-day builder (N4)


def daily_facts(language: str = "uk") -> str:
    """The envelope the core's #daily builder writes for the report of 2026-10-12."""
    daily = core_module("daily_report")
    reader = ScriptedFacts(
        (JOB_EVENT_QUERY, [{"payload": {"local_date": "2026-10-12"}}]),
        (RECIPIENT_QUERY, [recipient_row(language=language, telegram_user_id="777000", role="user")]),
        (SETTINGS_QUERY, [{"dictionary_language": "ru", "time_zone": "UTC", "notify_daily": True,
                           "daily_report_hour": 7, "daily_report_armed_at": datetime(2026, 10, 1, tzinfo=UTC),
                           "bot_opened": True}]),
        (FACTS_QUERY, [{"total": 20, "correct": 17}]))
    facts = daily.build(reader, JobContext(uuid4(), uuid4(), "daily_report:probe", "daily_report", 1, NOW))
    return facts.facts_json


def test_the_daily_report_the_core_builds_is_the_text_the_bot_renders():
    if not core_has_n4():
        pytest.skip("the core has no N4 (#daily of the local day)")
    raw = daily_facts("uk")
    envelope = json.loads(raw)
    assert (envelope["kind"], envelope["schema"]) == ("daily_report", 2)
    assert envelope["body"] == {"local_date": "2026-10-12", "total": 20, "correct": 17}
    prepared = discover().prepare("daily_report", raw)
    assert prepared.chat_id == 777000 and prepared.language == "uk"
    assert prepared.message.text.splitlines() == [
        "```text", "#daily", "Ретеншн (2026-10-12, перший дотик): 85.0%", "Вірно: 17/20", "80-90%: оптимально", "```"]
    assert "UTC" not in prepared.message.text


def test_every_kind_of_the_core_has_the_template_of_the_schema_its_builder_writes():
    registry = discover()
    checked = 0
    for kind in sorted(discover_kinds().names):
        schema = getattr(core_module(kind), "ENVELOPE_SCHEMA", None)
        if kind == "daily_report" and not core_has_n4():
            continue  # a core without N4 writes schema 1 with utc_date; the bot refuses that pair on purpose
        assert schema is not None, kind
        assert registry.template(kind).schema == schema, (kind, schema)
        checked += 1
    assert checked >= 8 or not core_has_n4()


def test_the_pairs_the_core_writes_are_the_pairs_the_bot_accepts_and_no_other():
    # The core's pairs (kind, schema) against the bot's: nothing the core writes is refused, nothing else is taken.
    registry = discover()
    core = {(kind, core_module(kind).ENVELOPE_SCHEMA) for kind in discover_kinds().names
            if kind != "daily_report" or core_has_n4()}
    bot = {(kind, registry.template(kind).schema) for kind in registry.kinds}
    assert core <= bot
    assert bot - core <= {("daily_report", 2), ("evening_reminder", 1), ("weekly_report", 1)}


def test_the_bot_refuses_the_daily_envelope_of_the_core_when_the_pair_is_changed():
    if not core_has_n4():
        pytest.skip("the core has no N4 (#daily of the local day)")
    envelope = json.loads(daily_facts("en"))
    registry = discover()
    for change, reason in ((("schema", 1), "schema_mismatch"), (("schema", 3), "schema_mismatch"),
                           (("schema", True), "schema_mismatch"), (("schema", "2"), "schema_mismatch"),
                           (("kind", "weekly_report"), "kind_mismatch")):
        broken = {**envelope, change[0]: change[1]}
        with pytest.raises(RenderError) as refused:
            registry.prepare("daily_report", json.dumps(broken))
        assert refused.value.reason == reason, change
    # The earlier core: schema 1 with utc_date -- refused whole, whatever the schema says.
    old = {**envelope, "schema": 1, "body": {"utc_date": "2026-10-12", "total": 20, "correct": 17}}
    with pytest.raises(RenderError) as refused:
        registry.prepare("daily_report", json.dumps(old))
    assert refused.value.reason == "schema_mismatch"
    still_old = {**envelope, "body": {"utc_date": "2026-10-12", "total": 20, "correct": 17}}
    with pytest.raises(RenderError) as refused:
        registry.prepare("daily_report", json.dumps(still_old))
    assert refused.value.reason == "body_keys"


def test_the_evening_reminder_the_core_builds_is_the_text_the_bot_renders(monkeypatch):
    pytest.importorskip("wortly.jobs.builders.evening_reminder")
    from wortly.jobs.builders import evening_reminder as core
    from wortly.jobs.builders._recipient import Recipient

    settings = {"time_zone": "UTC", "dictionary_language": "ru", "notify_evening": True,
                "evening_reminder_hour": 18, "bot_opened": True}
    monkeypatch.setattr(core, "recipient", lambda reader, profile: Recipient("active", "user", 1, "uk", 1, "777000", False))
    monkeypatch.setattr(core, "evening_settings", lambda reader, profile: settings)
    monkeypatch.setattr(core, "already_sent", lambda *args: False)
    monkeypatch.setattr(core, "ready_reviews", lambda *args: 7)
    monkeypatch.setattr(core, "learning_ready_now", lambda *args: 3)

    class Reader:
        def fetch_one(self, query, parameters):
            return {"payload": {"local_date": "2026-10-13"}}

    facts = core.build(Reader(), JobContext(uuid4(), uuid4(), "k", "evening_reminder", 1, datetime(2026, 10, 13, 19, tzinfo=UTC)))
    raw = facts.facts_json
    assert json.loads(raw)["schema"] == discover().template("evening_reminder").schema == 1
    prepared = discover().prepare("evening_reminder", raw)
    assert prepared.message.text.splitlines() == ["Нагадування про вивчення.", "Готово карток: 3", "Готово повторень: 7"]
    # The same envelope with another schema is not taken.
    for schema in (2, 0):
        with pytest.raises(RenderError) as refused:
            discover().prepare("evening_reminder", json.dumps({**json.loads(raw), "schema": schema}))
        assert refused.value.reason == "schema_mismatch"


# -- weekly_report: the core's real build (N4) -> envelope -> the bot's template ---------------------------------

WEEKLY_FACTS_QUERY = "SELECT count(f.card_id) AS written_cards"
WEEK_START = "2026-10-05"  # the reported week; its report's Monday is 2026-10-12 (Europe/Berlin, CEST = UTC+2)
ARMED_LONG_AGO = datetime(2026, 9, 1, tzinfo=UTC)
MONDAY_AFTER_THE_HOUR = datetime(2026, 10, 12, 8, tzinfo=UTC)  # 10:00 in Berlin, the hour 9 has passed


def weekly(now: datetime, *, armed=ARMED_LONG_AGO, notify: bool = True, hour: int = 9, language: str = "uk",
           facts: dict | None = None, blocked: bool = False):
    """What the core's weekly_report build returns for the week of 2026-10-05 at ``now``."""
    reader = ScriptedFacts(
        (JOB_EVENT_QUERY, [{"payload": {"week_start": WEEK_START}}]),
        (RECIPIENT_QUERY, [recipient_row(language=language, telegram_user_id="777000", role="user", blocked=blocked)]),
        (SETTINGS_QUERY, [{"time_zone": "Europe/Berlin", "dictionary_language": "uk", "notify_weekly": notify,
                           "weekly_report_hour": hour, "weekly_report_armed_at": armed, "bot_opened": True}]),
        (WEEKLY_FACTS_QUERY, [facts or {"study_actions": True, "written_cards": 12, "written_correct": 9}]))
    return core_module("weekly_report").build(
        reader, JobContext(uuid4(), uuid4(), "weekly_report:probe", "weekly_report", 1, now))


def weekly_envelope(now: datetime = MONDAY_AFTER_THE_HOUR, **arguments) -> tuple[str, dict]:
    """The envelope the core writes, its body asserted to be exactly the template's four facts before rendering."""
    from wortly_telegram.messages import weekly_report as template

    raw = weekly(now, **arguments).facts_json
    envelope = json.loads(raw)
    assert (envelope["kind"], envelope["schema"]) == ("weekly_report", discover().template("weekly_report").schema)
    body = envelope["body"]
    assert set(body) == template._FIELDS == {"week_start", "study_actions", "written_cards", "written_correct"}
    assert body["week_start"] == WEEK_START and body["study_actions"] is True
    assert type(body["written_cards"]) is int and type(body["written_correct"]) is int
    return raw, body


def test_the_weekly_report_the_core_builds_on_its_monday_is_the_text_the_bot_renders():
    raw, body = weekly_envelope()
    assert body == {"week_start": WEEK_START, "study_actions": True, "written_cards": 12, "written_correct": 9}
    prepared = discover().prepare("weekly_report", raw)
    assert prepared.chat_id == 777000 and prepared.language == "uk" and prepared.message.silent
    assert prepared.message.text.splitlines() == [
        "Тижневий звіт: 2026-10-05 – 2026-10-11", "Карток у письмових квізах: 12", "Вірно: 9", "Невірно: 3",
        "Частка вірних: 75,0%", "Порада: зменшити кількість нових слів."]
    for language in ("ru", "en"):
        raw, _ = weekly_envelope(language=language)
        assert discover().prepare("weekly_report", raw).language == language


def test_a_weekly_week_without_written_answers_is_the_sentence_without_a_share():
    raw, body = weekly_envelope(language="en", facts={"study_actions": True, "written_cards": 0, "written_correct": 0})
    assert (body["written_cards"], body["written_correct"]) == (0, 0)
    assert discover().prepare("weekly_report", raw).message.text.splitlines() == [
        "Weekly report: 2026-10-05 – 2026-10-11", "Written quiz cards: 0",
        "There were study actions this week, but no written answers."]


def test_the_weekly_report_waits_for_the_hour_of_the_setting_and_for_the_monday_end_when_off():
    before = weekly(datetime(2026, 10, 12, 6, tzinfo=UTC))  # 08:00 in Berlin, the hour is 9
    assert isinstance(before, Deferred) and before.until == datetime(2026, 10, 12, 7, tzinfo=UTC)
    off = weekly(MONDAY_AFTER_THE_HOUR, notify=False)  # switched off on the Monday: wait for its end
    assert isinstance(off, Deferred) and off.until == datetime(2026, 10, 12, 22, tzinfo=UTC)


def test_a_weekly_catch_up_after_the_monday_is_rendered_and_a_late_switch_on_is_no_message():
    wednesday = datetime(2026, 10, 14, 10, tzinfo=UTC)
    # Armed a minute before the Monday ended: the Monday was missed by a failure, the report is caught up.
    raw, _ = weekly_envelope(wednesday, armed=datetime(2026, 10, 12, 21, 59, tzinfo=UTC), language="ru",
                             facts={"study_actions": True, "written_cards": 5, "written_correct": 5})
    assert discover().prepare("weekly_report", raw).message.text.splitlines()[:5] == [
        "Недельный отчёт: 2026-10-05 – 2026-10-11", "Карточек в письменных квизах: 5", "Верно: 5", "Неверно: 0",
        "Доля верных: 100,0%"]
    # Armed on Tuesday 00:00 or later, or not stamped yet: a late switch-on, no report of last week.
    for armed in (datetime(2026, 10, 12, 22, tzinfo=UTC), datetime(2026, 10, 13, 9, tzinfo=UTC), None):
        assert weekly(wednesday, armed=armed) is None, armed
    # The window ends with the local Sunday after the report's Monday.
    assert weekly(datetime(2026, 10, 18, 21, 59, tzinfo=UTC)) is not None
    assert weekly(datetime(2026, 10, 18, 22, tzinfo=UTC)) is None


def test_a_weekly_week_without_study_actions_or_a_blocked_bot_is_no_message():
    assert weekly(MONDAY_AFTER_THE_HOUR, facts={"study_actions": False, "written_cards": 0, "written_correct": 0}) is None
    assert weekly(MONDAY_AFTER_THE_HOUR, blocked=True) is None


def test_the_bot_refuses_the_weekly_envelope_of_the_core_when_the_pair_or_the_body_is_changed():
    raw, body = weekly_envelope()
    envelope = json.loads(raw)
    registry = discover()
    for schema in (2, 0, True, "1"):
        with pytest.raises(RenderError) as refused:
            registry.prepare("weekly_report", json.dumps({**envelope, "schema": schema}))
        assert refused.value.reason == "schema_mismatch", schema
    renamed = {**{k: v for k, v in body.items() if k != "written_cards"}, "written_total": body["written_cards"]}
    for changed in ({**body, "pair": "uk-de"}, renamed, {k: v for k, v in body.items() if k != "written_correct"}):
        with pytest.raises(RenderError) as refused:
            registry.prepare("weekly_report", json.dumps({**envelope, "body": changed}))
        assert refused.value.reason == "body_keys", changed
