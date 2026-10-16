"""Templates: discovery, the contract with the core's job kinds, envelope checks, plural forms."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from wortly.jobs.kinds import discover as discover_kinds
from wortly_telegram.messages import LANGUAGES, REASONS, OutgoingMessage, RenderError, Template, reply_language
from wortly_telegram.messages.plural import category, plural
from wortly_telegram.messages.registry import (
    TemplateRegistry, TemplateRegistryError, discover,
)


def envelope(kind: str = "access_approved", *, language: str = "ru", recipient: object = None,
             body: object = None, schema: object = 1) -> str:
    return json.dumps({"schema": schema, "kind": kind, "ui_language": language,
                       "recipient": {"telegram_user_id": "4242"} if recipient is None else recipient,
                       "body": {} if body is None else body})


def test_every_core_job_kind_has_a_template_in_every_language():
    # The contract of design §4.3 п.2: a new kind without a template fails here, not in a delivery.
    registry = discover()
    kinds = discover_kinds().names
    assert kinds, "the core declares job kinds"
    assert registry.missing(kinds) == frozenset()
    for kind in kinds:
        template = registry.template(kind)
        for body in template.examples:
            for language in LANGUAGES:
                message = template.render(body, language)
                assert isinstance(message, OutgoingMessage)
                assert message.text.strip()


# Kinds whose template is ready before the core's builder exists. Remove a name when DEV-15 adds its builder.
AWAITING_CORE_BUILDER = frozenset({"weekly_report", "daily_report", "evening_reminder"})


def test_the_registry_has_no_template_for_a_kind_the_core_does_not_know():
    assert discover().kinds - AWAITING_CORE_BUILDER <= discover_kinds().names
    assert AWAITING_CORE_BUILDER <= discover().kinds


def test_access_approved_is_the_notice_with_the_app_button():
    prepared = discover().prepare("access_approved", envelope(language="en"))
    assert prepared.chat_id == 4242
    assert prepared.language == "en"
    assert prepared.message.app_button is True
    assert "Start" in prepared.message.text


@pytest.mark.parametrize("facts", [
    "not json",
    json.dumps([1, 2]),
    envelope(schema=2),
    envelope(schema=True),
    envelope(kind="study_reminder"),
    envelope(language="de"),
    envelope(language=None),
    envelope(recipient={}),
    envelope(recipient={"telegram_user_id": 4242}),
    envelope(recipient={"telegram_user_id": "04242"}),
    envelope(recipient={"telegram_user_id": str(2**52)}),
    envelope(recipient="4242"),
    envelope(body=[]),
    envelope(body={"unexpected": 1}),
], ids=["not-json", "not-object", "schema-2", "schema-bool", "other-kind", "language-de",
        "language-missing", "no-recipient-id", "recipient-int", "recipient-leading-zero",
        "recipient-too-large", "recipient-not-object", "body-list", "body-not-empty"])
def test_faulty_facts_fail_before_any_network_call(facts):
    with pytest.raises(RenderError):
        discover().prepare("access_approved", facts)


# The compatible pairs (kind, schema): what the core's builders write (ENVELOPE_SCHEMA of each), as a list.
COMPATIBLE_PAIRS = {
    ("access_approved", 1), ("access_reminder", 1), ("access_request", 1), ("inbox_message", 1),
    ("study_reminder", 1), ("evening_reminder", 1), ("weekly_report", 1), ("daily_report", 2),
}


def test_the_compatible_pairs_of_kind_and_schema_are_exactly_these():
    registry = discover()
    assert {(kind, registry.template(kind).schema) for kind in registry.kinds} == COMPATIBLE_PAIRS


@pytest.mark.parametrize(("kind", "schema", "body"), [
    ("daily_report", 1, {"local_date": "2026-10-12", "total": 20, "correct": 17}),
    ("daily_report", 1, {"utc_date": "2026-10-12", "total": 20, "correct": 17}),
    ("daily_report", 3, {"local_date": "2026-10-12", "total": 20, "correct": 17}),
    ("daily_report", True, {"local_date": "2026-10-12", "total": 20, "correct": 17}),
    ("daily_report", "2", {"local_date": "2026-10-12", "total": 20, "correct": 17}),
    ("daily_report", None, {"local_date": "2026-10-12", "total": 20, "correct": 17}),
    ("evening_reminder", 2, {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": "reviews_ready", "ready": 1}]}),
    ("study_reminder", 2, {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": "learning_ready", "ready": 1}]}),
    ("weekly_report", 2, {}),
    ("access_approved", 2, {}),
], ids=["daily-1", "daily-1-utc", "daily-3", "daily-bool", "daily-str", "daily-none", "evening-2", "study-2",
        "weekly-2", "approved-2"])
def test_a_pair_outside_the_list_is_refused_before_any_network_call(kind, schema, body):
    with pytest.raises(RenderError) as refused:
        discover().prepare(kind, envelope(kind, schema=schema, body=body))
    assert refused.value.reason == "schema_mismatch"


def test_the_daily_envelope_of_schema_2_is_prepared_and_the_other_kinds_keep_schema_1():
    body = {"local_date": "2026-10-12", "total": 20, "correct": 17}
    assert discover().prepare("daily_report", envelope("daily_report", schema=2, body=body)).chat_id == 4242
    for kind in COMPATIBLE_PAIRS:
        if kind[0] != "daily_report":
            assert discover().template(kind[0]).schema == 1


@pytest.mark.parametrize(("facts", "reason"), [
    ("not json", "not_json"),
    (json.dumps([1]), "envelope_shape"),
    (envelope(schema=2), "schema_mismatch"),
    (envelope(kind="study_reminder"), "kind_mismatch"),
    (envelope(language="de"), "language_unsupported"),
    (envelope(recipient={}), "recipient_invalid"),
    (envelope(recipient="4242"), "recipient_invalid"),
    (envelope(body=[]), "body_invalid"),
    (envelope(body={"unexpected": 1}), "render_invalid"),
], ids=["not-json", "not-object", "schema", "kind", "language", "recipient", "recipient-not-object", "body-list",
        "body-keys"])
def test_a_refusal_names_a_bounded_category(facts, reason):
    with pytest.raises(RenderError) as refused:
        discover().prepare("access_approved", facts)
    assert refused.value.reason == reason and reason in REASONS


def test_an_unknown_kind_is_the_category_unknown_kind():
    with pytest.raises(RenderError) as refused:
        discover().prepare("no_such_kind", envelope(kind="no_such_kind"))
    assert refused.value.reason == "unknown_kind"


def test_a_body_with_other_keys_is_the_category_body_keys():
    with pytest.raises(RenderError) as refused:
        discover().prepare("daily_report", envelope("daily_report", schema=2, body={"utc_date": "2026-10-12", "total": 1, "correct": 1}))
    assert refused.value.reason == "body_keys"


def test_a_reason_outside_the_list_and_a_schema_outside_the_domain_are_refused_to_declare():
    with pytest.raises(ValueError):
        RenderError("x", reason="made up")
    assert RenderError("x").reason == "render_invalid"
    for schema in (0, -1, True, "2", None, 1.0):
        with pytest.raises(ValueError):
            Template("x_kind", lambda body, language: OutgoingMessage("t"), examples=({},), schema=schema)


def test_an_unknown_kind_has_no_template():
    with pytest.raises(RenderError):
        discover().prepare("no_such_kind", envelope(kind="no_such_kind"))


def test_a_template_fault_becomes_a_render_error_without_its_text():
    def broken(body, language):
        raise KeyError("secret text of the message")

    registry = TemplateRegistry([Template("x_kind", broken, examples=({},))])
    with pytest.raises(RenderError) as raised:
        registry.prepare("x_kind", envelope(kind="x_kind"))
    assert "secret" not in str(raised.value)


def test_duplicate_kinds_are_refused():
    template = Template("x_kind", lambda body, language: OutgoingMessage("t"), examples=({},))
    with pytest.raises(TemplateRegistryError):
        TemplateRegistry([template, template])


def test_a_module_without_a_template_stops_discovery(tmp_path, monkeypatch):
    package = tmp_path / "probe_templates"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "_helper.py").write_text("VALUE = 1\n")
    (package / "good.py").write_text(textwrap.dedent("""
        from wortly_telegram.messages import OutgoingMessage, Template
        TEMPLATE = Template("good_kind", lambda body, language: OutgoingMessage("t"), examples=({},))
    """))
    monkeypatch.syspath_prepend(str(tmp_path))
    assert discover("probe_templates").kinds == {"good_kind"}
    (package / "bad.py").write_text("VALUE = 2\n")
    for name in [name for name in sys.modules if name.startswith("probe_templates")]:
        monkeypatch.delitem(sys.modules, name)
    with pytest.raises(TemplateRegistryError):
        discover("probe_templates")


@pytest.mark.parametrize(("language", "count", "expected"), [
    ("ru", 1, "one"), ("ru", 2, "few"), ("ru", 4, "few"), ("ru", 5, "many"), ("ru", 0, "many"),
    ("ru", 11, "many"), ("ru", 12, "many"), ("ru", 14, "many"), ("ru", 21, "one"), ("ru", 22, "few"),
    ("ru", 111, "many"), ("ru", 101, "one"), ("uk", 1, "one"), ("uk", 3, "few"), ("uk", 13, "many"),
    ("en", 1, "one"), ("en", 0, "other"), ("en", 2, "other"), ("en", 21, "other"),
])
def test_plural_categories(language, count, expected):
    assert category(language, count) == expected


def test_plural_picks_the_form_and_requires_every_form_of_the_language():
    forms = {"one": "слово", "few": "слова", "many": "слов"}
    assert plural("ru", 3, forms) == "слова"
    assert plural("ru", 25, forms) == "слов"
    with pytest.raises(RenderError):
        plural("ru", 3, {"one": "слово", "many": "слов"})
    with pytest.raises(RenderError):
        category("ru", -1)


@pytest.mark.parametrize(("saved", "telegram", "expected"), [
    ("uk", "ru", "uk"), (None, "ru-RU", "ru"), (None, None, "en"), ("de", "uk", "uk"), (None, "fr", "en"),
])
def test_reply_language_follows_the_core_rule(saved, telegram, expected):
    assert reply_language(saved, telegram) == expected
