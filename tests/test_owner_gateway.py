"""The gateway's owner editor calls (project DEV-17 §4.2, stage B1a) on the core's internal API.

FakeCore answers behind httpx.MockTransport; no socket. Each call is one POST to its fixed
route with the body the internal schema names; an envelope refusal keeps the core's code and
status; any answer outside the schema (docs/openapi_internal.json) is CoreUnavailable; the
``b.core`` line names the route template, never a body value.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from fakes.core_gateway import CORE_SECRET, FakeCore, core_settings, envelope, forbid_network, owner_card_wire
from wortly.identity.principal import TelegramPrincipal
from wortly_telegram.gateway import (
    CoreError, CoreUnavailable, GrammarForm, OwnerCard, OwnerCommand, OwnerSearch, OwnerSearchCard, build_gateway,
)


OWNER = TelegramPrincipal("1001")
CARD = UUID("6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5")
COMMAND = UUID("0b6e1a52-7f61-4c3e-9d2a-3b4c5d6e7f80")
SEARCH, READ, TRANSLATION, GRAMMAR = ("/internal/v1/owner/cards/search", "/internal/v1/owner/cards/read",
                                      "/internal/v1/owner/cards/translation", "/internal/v1/owner/cards/grammar")
SCHEMA = Path(__file__).resolve().parents[2] / "core" / "docs" / "openapi_internal.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


@pytest.fixture
def core() -> FakeCore:
    core = FakeCore()
    core.summaries["1001"] = {"role": "owner", "status": "admitted", "ui_language": "ru"}
    core.cards[str(CARD)] = owner_card_wire(str(CARD))
    return core


@pytest.fixture
async def gateway(core):
    current = build_gateway(core_settings(), transport=core.transport())
    yield current
    await current.aclose()


def fact(status: str = "applied", *, command: UUID = COMMAND, version: str = "8") -> dict:
    return {"id": str(command), "status": status, "applied_at": "2026-10-15T12:00:00Z", "target_id": str(CARD),
            "object_version": version}


async def every_owner_call(gateway) -> None:
    await gateway.owner_search(OWNER, query="haus")
    await gateway.owner_card(OWNER, card_id=CARD)
    await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="move_up", index=2,
                                 text=None, expected_card_revision=7)
    await gateway.grammar_save(OWNER, command_id=uuid4(), card_id=CARD, field_name="plural",
                               value={"status": "applicable", "value": "Häuser"}, expected_card_revision=8)


# -- requests ------------------------------------------------------------------------------

async def test_each_owner_call_is_one_post_to_its_route_with_the_secret_and_the_schema_body(core, gateway):
    await every_owner_call(gateway)
    assert core.paths() == [SEARCH, READ, TRANSLATION, GRAMMAR]
    assert all(request.method == "POST" and request.headers.get_list("x-wortly-service-secret") == [CORE_SECRET]
               for request in core.requests)
    assert core.bodies(SEARCH) == [{"actor_telegram_id": "1001", "query": "haus", "limit": 10}]
    assert core.bodies(READ) == [{"actor_telegram_id": "1001", "card_id": str(CARD)}]
    assert core.bodies(TRANSLATION) == [{"actor_telegram_id": "1001", "command_id": str(COMMAND),
                                         "card_id": str(CARD), "language": "ru", "op": "move_up", "index": 2,
                                         "expected_card_revision": "7"}]
    [grammar] = core.bodies(GRAMMAR)
    assert grammar["value"] == {"status": "applicable", "value": "Häuser"}
    assert grammar["field_name"] == "plural" and grammar["expected_card_revision"] == "8"


async def test_the_bodies_have_exactly_the_fields_of_the_core_schema(core, gateway):
    await gateway.owner_search(OWNER, query="haus", limit=3, card_type="noun")
    await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="uk", op="append", index=None,
                                 text="хата", expected_card_revision=7)
    await gateway.translation_op(OWNER, command_id=uuid4(), card_id=CARD, language="ru", op="replace", index=1,
                                 text="домик", expected_card_revision=8)
    await gateway.grammar_save(OWNER, command_id=uuid4(), card_id=CARD, field_name="article",
                               value={"status": "unknown", "value": None}, expected_card_revision=9)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    paths, components = schema["paths"], schema["components"]["schemas"]
    for path, model in ((SEARCH, "OwnerSearchRequest"), (READ, "OwnerCardRequest"),
                        (TRANSLATION, "TranslationOperationRequest"), (GRAMMAR, "GrammarSaveRequest")):
        assert path in paths
        reference = paths[path]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert reference.endswith(f"/{model}")
        properties, required = set(components[model]["properties"]), set(components[model].get("required", ()))
        for body in core.bodies(path):
            assert required <= set(body) <= properties, (path, body)
    [search] = core.bodies(SEARCH)
    assert search["type"] == "noun" and search["limit"] == 3
    append, replace = core.bodies(TRANSLATION)
    assert "index" not in append and append["text"] == "хата"
    assert (replace["index"], replace["text"]) == (1, "домик")


async def test_the_same_arguments_give_the_same_body(core, gateway):
    # The body is a function of the arguments only (§4.4): nothing from an earlier answer.
    for _ in range(2):
        await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="remove", index=2,
                                     text=None, expected_card_revision=7)
        core.cards[str(CARD)]["card_revision"] = "20"
    first, second = core.bodies(TRANSLATION)
    assert first == second


@pytest.mark.parametrize("arguments", [
    {"language": "de"}, {"op": "insert"}, {"index": 0}, {"index": 100}, {"index": True}, {"text": 5},
])
async def test_a_malformed_translation_command_never_leaves_the_bot(core, gateway, arguments):
    values = dict(command_id=COMMAND, card_id=CARD, language="ru", op="remove", index=1, text=None,
                  expected_card_revision=7) | arguments
    with pytest.raises((ValueError, TypeError)):
        await gateway.translation_op(OWNER, **values)
    assert core.requests == []


@pytest.mark.parametrize("arguments", [{"query": ""}, {"query": "x" * 201}, {"limit": 0}, {"limit": 11}])
async def test_a_malformed_search_never_leaves_the_bot(core, gateway, arguments):
    with pytest.raises(ValueError):
        await gateway.owner_search(OWNER, **({"query": "haus"} | arguments))
    assert core.requests == []


# -- answers -------------------------------------------------------------------------------

async def test_the_answers_become_the_editor_objects(core, gateway):
    found = await gateway.owner_search(OWNER, query="haus")
    assert found == OwnerSearch((OwnerSearchCard(CARD, "das Haus", "noun", "A1", "visible", "дом"),), False)
    card = await gateway.owner_card(OWNER, card_id=CARD)
    assert card == OwnerCard(CARD, "noun", "das Haus", "A1", 7, "ru", "visible",
                             {"ru": ("дом", "здание"), "uk": ("будинок",), "en": ()},
                             {"gender": GrammarForm("applicable", "neuter"), "singular": GrammarForm("applicable", "Haus"),
                              "plural": GrammarForm("applicable", "Häuser"), "article": GrammarForm("applicable")})
    done = await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="move_down",
                                        index=1, text=None, expected_card_revision=7)
    assert (done.status, done.revision, done.current.translations["ru"]) == ("applied", 8, ("здание", "дом"))
    again = await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="move_down",
                                         index=1, text=None, expected_card_revision=7)
    assert (again.status, again.revision) == ("already_applied", 8)


async def test_a_verb_and_an_adjective_and_a_card_without_grammar(core, gateway):
    for card_type in ("verb", "adjective", "phrase"):
        card_id = uuid4()
        core.cards[str(card_id)] = owner_card_wire(str(card_id), card_type=card_type)
        card = await gateway.owner_card(OWNER, card_id=card_id)
        if card_type == "verb":
            assert card.grammar["separability"] == "inseparable"
            assert card.grammar["auxiliary"] == GrammarForm("applicable", "sein")
        elif card_type == "adjective":
            assert set(card.grammar) == {"positive", "comparative", "superlative"}
        else:
            assert card.grammar is None


async def test_a_repeat_after_a_demotion_has_no_card(core, gateway):
    core.answer(TRANSLATION, httpx.Response(200, json={"command": fact("already_applied"), "current": None}))
    done = await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="remove",
                                        index=1, text=None, expected_card_revision=7)
    assert done == OwnerCommand("already_applied", 8, None)


@pytest.mark.parametrize(("status", "code"), [(403, "FORBIDDEN"), (409, "VERSION_CONFLICT"),
                                              (409, "WRITE_UNAVAILABLE"), (409, "COMMAND_PAYLOAD_MISMATCH"),
                                              (422, "VALIDATION_FAILED")])
async def test_an_envelope_refusal_keeps_the_core_code_and_status(core, gateway, status, code):
    for path in (SEARCH, READ, TRANSLATION, GRAMMAR):
        core.answer(path, envelope(status, code))
    for call in (gateway.owner_search(OWNER, query="haus"), gateway.owner_card(OWNER, card_id=CARD),
                 gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="remove",
                                        index=1, text=None, expected_card_revision=7),
                 gateway.grammar_save(OWNER, command_id=COMMAND, card_id=CARD, field_name="article",
                                      value={"status": "unknown", "value": None}, expected_card_revision=7)):
        with pytest.raises(CoreError) as raised:
            await call
        assert not isinstance(raised.value, CoreUnavailable)
        assert (raised.value.status, raised.value.code) == (status, code)
    assert len(core.requests) == 4


def _card(**changes) -> dict:
    return owner_card_wire(str(CARD)) | changes


def _grammar(**changes) -> dict:
    return _card(grammar=owner_card_wire(str(CARD))["grammar"] | changes)


OUTSIDE_CARD = [
    _card(extra=1), {key: value for key, value in _card().items() if key != "state"},
    _card(card_id="not-a-uuid"), _card(card_id=str(uuid4())), _card(card_type="article"), _card(lemma=None),
    _card(cefr="D1"), _card(card_revision="0"), _card(card_revision=7), _card(language="de"),
    _card(state="unpublished"), _card(translations={"ru": ["дом"], "uk": []}),
    _card(translations={"ru": ["дом", 3], "uk": [], "en": []}), _card(translations={"ru": "дом", "uk": [], "en": []}),
    _grammar(gender="xyz"), _grammar(gender_status="maybe"), _grammar(article="der"), _grammar(plural=5),
    _card(grammar={"infinitive": "gehen"}), _card(card_type="phrase"),
]


@pytest.mark.parametrize("current", OUTSIDE_CARD)
async def test_a_card_outside_the_contract_is_core_unavailable(core, gateway, current):
    core.answer(READ, httpx.Response(200, json={"current": current}))
    core.answer(TRANSLATION, httpx.Response(200, json={"command": fact(), "current": current}))
    with pytest.raises(CoreUnavailable):
        await gateway.owner_card(OWNER, card_id=CARD)
    with pytest.raises(CoreUnavailable):
        await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="remove", index=1,
                                     text=None, expected_card_revision=7)


@pytest.mark.parametrize("answer", [
    {"command": fact(), "current": _card(), "extra": 1}, {"command": fact()},
    {"command": fact(command=uuid4()), "current": _card()}, {"command": fact("queued"), "current": _card()},
    {"command": fact(version="0"), "current": _card()}, {"command": {**fact(), "applied_at": None}, "current": _card()},
    {"command": {**fact(), "target_id": 5}, "current": _card()},
    {"command": {k: v for k, v in fact().items() if k != "target_id"}, "current": _card()},
])
async def test_a_command_answer_outside_the_contract_is_core_unavailable(core, gateway, answer):
    core.answer(GRAMMAR, httpx.Response(200, json=answer))
    with pytest.raises(CoreUnavailable):
        await gateway.grammar_save(OWNER, command_id=COMMAND, card_id=CARD, field_name="separability",
                                   value="separable", expected_card_revision=7)


@pytest.mark.parametrize("answer", [
    {"cards": [], "more": "no"}, {"cards": {}, "more": False}, {"cards": [], "more": False, "next": None},
    {"cards": [{"card_id": str(CARD), "lemma": "x", "type": "noun", "cefr": None, "state": "visible"}],
     "more": False},
    {"cards": [{"card_id": str(CARD), "lemma": "x", "type": "noun", "cefr": None, "state": "unpublished",
                "preview": None}], "more": False},
    {"cards": [{"card_id": str(uuid4()), "lemma": "x", "type": "noun", "cefr": None, "state": "visible",
                "preview": None}] * 2, "more": False},
])
async def test_a_search_answer_outside_the_contract_is_core_unavailable(core, gateway, answer):
    core.answer(SEARCH, httpx.Response(200, json=answer))
    with pytest.raises(CoreUnavailable):
        await gateway.owner_search(OWNER, query="haus", limit=1)


@pytest.mark.parametrize("answer", [httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR"}}),
                                    httpx.Response(200, content=b"not json"), httpx.Response(204),
                                    httpx.Response(409, json={"detail": "proxy"}), httpx.ConnectError("refused")])
async def test_no_usable_answer_is_core_unavailable_and_nothing_is_repeated(core, gateway, answer):
    core.answer(TRANSLATION, answer)
    with pytest.raises(CoreUnavailable):
        await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="remove", index=1,
                                     text=None, expected_card_revision=7)
    assert core.paths() == [TRANSLATION]


# -- trail -----------------------------------------------------------------------------------

async def test_the_core_lines_name_the_route_template_and_no_body_value(core, gateway, trail_lines):
    core.answer(GRAMMAR, envelope(422, "VALIDATION_FAILED"))
    await gateway.owner_search(OWNER, query="Häuschen-secret-query")
    await gateway.owner_card(OWNER, card_id=CARD)
    await gateway.translation_op(OWNER, command_id=COMMAND, card_id=CARD, language="ru", op="replace", index=1,
                                 text="текст-перевода-secret", expected_card_revision=7)
    with pytest.raises(CoreError):
        await gateway.grammar_save(OWNER, command_id=uuid4(), card_id=CARD, field_name="plural",
                                   value={"status": "applicable", "value": "Formen-secret"}, expected_card_revision=8)
    lines = trail_lines.events("b.core")
    assert [line["route"] for line in lines] == [SEARCH, READ, TRANSLATION, GRAMMAR]
    assert [line.get("status") for line in lines] == [200, 200, 200, 422]
    assert lines[-1]["code"] == "VALIDATION_FAILED"
    everything = trail_lines.everything
    for value in ("Häuschen", "secret", str(CARD), str(COMMAND), "1001", "дом", "das Haus"):
        assert value not in everything
