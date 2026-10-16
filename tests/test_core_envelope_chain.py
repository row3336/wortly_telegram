"""The chain builder -> envelope -> renderer against the core's own builders (N-AC33, N-AC41; project_notifications ed.4 §7.3, §7.5).

The core's real ``build`` of a kind (over scripted rows, no database) writes the envelope, and the bot's registry
prepares exactly that text. The pair (kind, schema) of each template is the one its builder writes (ENVELOPE_SCHEMA):
evening_reminder 1, #daily 2 once the core has N4. A core without N3 or N4 skips what it cannot write; with them
every test here runs. Checked on the core ceec5c9 + the cumulative candidate notif_n3n5n4 (see the worklog).
"""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
import json
from uuid import uuid4

import pytest

from fakes.core_gateway import JOB_EVENT_QUERY, RECIPIENT_QUERY, ScriptedFacts, recipient_row
from wortly.jobs.kinds import discover as discover_kinds
from wortly.jobs.models import JobContext
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
