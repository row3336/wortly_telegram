"""The gateway's synonym calls and the C4 decoder (project DEV-17 §4.2, stage B1b; release gate C334).

The core C4 adds ``OwnerCard.synonyms`` and ``OwnerSearchCard.synonym_group_version``. The decoder
knows these fields exactly, as every other field: an answer without them (a core before C4) or
with anything else is outside the contract (CoreUnavailable). The request bodies and the answer
shapes are compared with the core's offline schema docs/openapi_internal.json (C4).
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
    CoreError, CoreUnavailable, OwnerSearchCard, SynonymCommand, SynonymGroup, SynonymList, SynonymMember,
    build_gateway,
)


OWNER = TelegramPrincipal("1001")
CARD, OTHER, THIRD = (UUID("10000000-0000-4000-8000-000000000001"), UUID("20000000-0000-4000-8000-000000000002"),
                      UUID("30000000-0000-4000-8000-000000000003"))
COMMAND = UUID("0b6e1a52-7f61-4c3e-9d2a-3b4c5d6e7f80")
P = "/internal/v1/owner/synonyms/"
LINK, LINK_MANY, UNLINK, CLEAR, CANDIDATES = (P + "link", P + "link_many", P + "unlink", P + "clear",
                                              P + "candidates")
READ, SEARCH = "/internal/v1/owner/cards/read", "/internal/v1/owner/cards/search"
SCHEMA = Path(__file__).resolve().parents[2] / "core" / "docs" / "openapi_internal.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


@pytest.fixture
def core() -> FakeCore:
    core = FakeCore()
    core.summaries["1001"] = {"role": "owner", "status": "admitted", "ui_language": "ru"}
    core.cards[str(CARD)] = owner_card_wire(str(CARD), lemma="das Haus")
    core.cards[str(OTHER)] = owner_card_wire(str(OTHER), lemma="das Gebäude", translations={"ru": ["здание"]})
    core.cards[str(THIRD)] = owner_card_wire(str(THIRD), lemma="der Bau", translations={"ru": ["здание"]})
    return core


@pytest.fixture
async def gateway(core):
    current = build_gateway(core_settings(), transport=core.transport())
    yield current
    await current.aclose()


def components() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))["components"]["schemas"]


def properties(model: str) -> set[str]:
    return set(components()[model]["properties"])


async def every_synonym_call(gateway, core) -> None:
    await gateway.synonym_candidates(OWNER, card_id=CARD)
    await gateway.synonym_link(OWNER, command_id=COMMAND, card_id=CARD, other_card_id=OTHER,
                               expected_group_version=None, expected_other_group_version=None)
    version = int(core._version_of(str(CARD)))
    await gateway.synonym_link_many(OWNER, command_id=uuid4(), card_id=CARD, other_card_ids=(THIRD,),
                                    expected_group_version=version)
    version = int(core._version_of(str(CARD)))
    await gateway.synonym_unlink(OWNER, command_id=uuid4(), card_id=CARD, index=3, expected_group_version=version)
    version = int(core._version_of(str(CARD)))
    await gateway.synonym_clear(OWNER, command_id=uuid4(), card_id=CARD, expected_group_version=version)


# -- requests --------------------------------------------------------------------------------

async def test_each_synonym_call_is_one_post_with_the_schema_body(core, gateway):
    await every_synonym_call(gateway, core)
    assert core.paths() == [CANDIDATES, LINK, LINK_MANY, UNLINK, CLEAR]
    assert all(request.method == "POST" and request.headers.get_list("x-wortly-service-secret") == [CORE_SECRET]
               for request in core.requests)
    assert core.bodies(CANDIDATES) == [{"actor_telegram_id": "1001", "card_id": str(CARD)}]
    assert core.bodies(LINK) == [{"actor_telegram_id": "1001", "command_id": str(COMMAND), "card_id": str(CARD),
                                  "other_card_id": str(OTHER), "expected_group_version": None,
                                  "expected_other_group_version": None}]
    [many] = core.bodies(LINK_MANY)
    assert many["other_card_ids"] == [str(THIRD)] and many["expected_group_version"].isdigit()
    [unlink] = core.bodies(UNLINK)
    assert unlink["index"] == 3 and unlink["expected_group_version"].isdigit()
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for path, model in ((LINK, "SynonymLinkRequest"), (LINK_MANY, "SynonymLinkManyRequest"),
                        (UNLINK, "SynonymUnlinkRequest"), (CLEAR, "SynonymClearRequest"),
                        (CANDIDATES, "SynonymCandidatesRequest")):
        reference = schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert reference.endswith(f"/{model}")
        required = set(components()[model].get("required", ()))
        for body in core.bodies(path):
            assert required <= set(body) <= properties(model), (path, body)


@pytest.mark.parametrize(("call", "arguments"), [
    ("synonym_link_many", {"other_card_ids": ()}), ("synonym_link_many", {"other_card_ids": (OTHER,) * 21}),
    ("synonym_unlink", {"index": 0}), ("synonym_unlink", {"index": 100}),
    ("synonym_unlink", {"expected_group_version": 0}), ("synonym_clear", {"expected_group_version": None}),
    ("synonym_link", {"expected_group_version": 0}),
])
async def test_a_malformed_synonym_command_never_leaves_the_bot(core, gateway, call, arguments):
    defaults = {"synonym_link_many": dict(other_card_ids=(OTHER,), expected_group_version=5),
                "synonym_unlink": dict(index=1, expected_group_version=5),
                "synonym_clear": dict(expected_group_version=5),
                "synonym_link": dict(other_card_id=OTHER, expected_group_version=None,
                                     expected_other_group_version=None)}[call]
    with pytest.raises((ValueError, TypeError)):
        await getattr(gateway, call)(OWNER, command_id=COMMAND, card_id=CARD, **(defaults | arguments))
    assert core.requests == []


# -- answers: the C4 decoder --------------------------------------------------------------------

def test_the_fake_core_answers_in_the_c4_schema(core):
    # The doubles the bot's tests use carry exactly the fields of the C4 schema.
    core.link_cards(str(CARD), str(OTHER))
    card = core._card_out(core.cards[str(CARD)])
    assert set(card) == properties("OwnerCard")
    assert set(card["synonyms"]) == properties("SynonymList")
    assert all(set(member) == properties("SynonymMember") for member in card["synonyms"]["members"])
    assert set(core._group_out(str(CARD))) == properties("SynonymGroup")
    search = json.loads(core._search({"query": "das", "limit": 10}).content)
    assert all(set(item) == properties("OwnerSearchCard") for item in search["cards"])
    assert properties("SynonymCommandResponse") == {"command", "current"}
    assert properties("SynonymCandidatesResponse") == {"cards", "more"}
    # Every field is required: the decoder may insist on each.
    for model in ("OwnerCard", "OwnerSearchCard", "SynonymList", "SynonymMember", "SynonymGroup"):
        assert set(components()[model]["required"]) == properties(model), model


async def test_the_card_and_the_search_carry_the_group(core, gateway):
    version = core.link_cards(str(CARD), str(OTHER))
    card = await gateway.owner_card(OWNER, card_id=CARD)
    assert card.synonyms == SynonymList(version, (
        SynonymMember(CARD, "das Haus", "noun", "A1", "visible", "дом"),
        SynonymMember(OTHER, "das Gebäude", "noun", "A1", "visible", "здание")))
    found = await gateway.owner_search(OWNER, query="der bau")
    assert found.cards == (OwnerSearchCard(THIRD, "der Bau", "noun", "A1", "visible", "здание", None),)
    found = await gateway.owner_search(OWNER, query="gebäude")
    assert found.cards[0].group_version == version


async def test_the_commands_answer_the_group(core, gateway):
    done = await gateway.synonym_link(OWNER, command_id=COMMAND, card_id=CARD, other_card_id=OTHER,
                                      expected_group_version=None, expected_other_group_version=None)
    version = int(core._version_of(str(CARD)))
    assert done == SynonymCommand("applied", version, SynonymGroup(CARD, "noun", SynonymList(version, (
        SynonymMember(CARD, "das Haus", "noun", "A1", "visible", "дом"),
        SynonymMember(OTHER, "das Gebäude", "noun", "A1", "visible", "здание")))))
    again = await gateway.synonym_link(OWNER, command_id=COMMAND, card_id=CARD, other_card_id=OTHER,
                                       expected_group_version=None, expected_other_group_version=None)
    assert (again.status, again.version) == ("already_applied", version)
    cleared = await gateway.synonym_clear(OWNER, command_id=uuid4(), card_id=CARD, expected_group_version=version)
    assert cleared.current.synonyms == SynonymList(None, ()) and cleared.version > version
    candidates = await gateway.synonym_candidates(OWNER, card_id=OTHER)
    # By lemma: both share "здание" with the other card.
    assert [card.card_id for card in candidates.cards] == [CARD, THIRD] and candidates.more is False


async def test_a_repeat_after_a_demotion_has_no_group(core, gateway):
    fact = {"id": str(COMMAND), "status": "already_applied", "applied_at": "2026-10-15T12:00:00Z",
            "target_id": str(CARD), "object_version": "9"}
    core.answer(CLEAR, httpx.Response(200, json={"command": fact, "current": None}))
    assert await gateway.synonym_clear(OWNER, command_id=COMMAND, card_id=CARD,
                                       expected_group_version=9) == SynonymCommand("already_applied", 9, None)


def _old(card: dict) -> dict:
    return {key: value for key, value in card.items() if key != "synonyms"}


async def test_an_answer_of_a_core_before_c4_is_outside_the_contract(core, gateway):
    # Release gate C334: the bot is released with C4; the ceec5c9 shape is refused as a whole.
    card = core._card_out(core.cards[str(CARD)])
    core.answer(READ, httpx.Response(200, json={"current": _old(card)}))
    with pytest.raises(CoreUnavailable):
        await gateway.owner_card(OWNER, card_id=CARD)
    search = json.loads(core._search({"query": "haus", "limit": 10}).content)
    old = [{key: value for key, value in item.items() if key != "synonym_group_version"} for item in search["cards"]]
    core.answer(SEARCH, httpx.Response(200, json={"cards": old, "more": False}))
    with pytest.raises(CoreUnavailable):
        await gateway.owner_search(OWNER, query="haus")


def _card(**synonyms) -> dict:
    return owner_card_wire(str(CARD)) | {"synonyms": {"group_version": None, "members": []} | synonyms}


MEMBER = {"card_id": str(OTHER), "lemma": "das Gebäude", "type": "noun", "cefr": None, "state": "visible",
          "preview": "здание"}
OUTSIDE = [
    _card(extra=1), {**owner_card_wire(str(CARD)), "synonyms": {"members": []}}, _card(group_version=7),
    _card(group_version="7", members=[MEMBER, MEMBER]), _card(group_version=None, members=[MEMBER]),
    _card(group_version="0", members=[MEMBER]), _card(group_version="7", members=[{**MEMBER, "tag": 1}]),
    _card(group_version="7", members=[{**MEMBER, "type": "word"}]),
    _card(group_version="7", members=[{**MEMBER, "state": "unpublished"}]),
    _card(group_version="7", members=[{**MEMBER, "card_id": "x"}]), _card(group_version="7", members="x"),
    {**owner_card_wire(str(CARD)), "synonyms": None},
]


@pytest.mark.parametrize("current", OUTSIDE)
async def test_a_group_outside_the_contract_is_core_unavailable(core, gateway, current):
    core.answer(READ, httpx.Response(200, json={"current": current}))
    with pytest.raises(CoreUnavailable):
        await gateway.owner_card(OWNER, card_id=CARD)


def _fact(command: UUID = COMMAND, version: str = "9") -> dict:
    return {"id": str(command), "status": "applied", "applied_at": "2026-10-15T12:00:00Z", "target_id": str(CARD),
            "object_version": version}


def _group(**changes) -> dict:
    return {"card_id": str(CARD), "card_type": "noun", "group_version": None, "members": []} | changes


@pytest.mark.parametrize("answer", [
    {"command": _fact(), "current": _group(extra=1)}, {"command": _fact(), "current": _group(card_id=str(OTHER))},
    {"command": _fact(), "current": _group(card_type="word")},
    {"command": _fact(), "current": {k: v for k, v in _group().items() if k != "card_type"}},
    {"command": _fact(command=uuid4()), "current": _group()}, {"command": _fact(version="0"), "current": _group()},
    {"command": _fact(), "current": owner_card_wire(str(CARD))}, {"command": _fact()},
])
async def test_a_synonym_command_answer_outside_the_contract_is_core_unavailable(core, gateway, answer):
    core.answer(CLEAR, httpx.Response(200, json=answer))
    with pytest.raises(CoreUnavailable):
        await gateway.synonym_clear(OWNER, command_id=COMMAND, card_id=CARD, expected_group_version=9)


@pytest.mark.parametrize("answer", [
    {"cards": [], "more": None}, {"cards": [{**MEMBER, "synonym_group_version": "0"}], "more": False},
    {"cards": [MEMBER], "more": False}, {"cards": [{**MEMBER, "synonym_group_version": None}] * 21, "more": True},
])
async def test_candidates_outside_the_contract_are_core_unavailable(core, gateway, answer):
    core.answer(CANDIDATES, httpx.Response(200, json=answer))
    with pytest.raises(CoreUnavailable):
        await gateway.synonym_candidates(OWNER, card_id=CARD)


@pytest.mark.parametrize(("status", "code"), [(403, "FORBIDDEN"), (409, "VERSION_CONFLICT"),
                                              (409, "WRITE_UNAVAILABLE"), (422, "VALIDATION_FAILED")])
async def test_an_envelope_refusal_keeps_its_code(core, gateway, status, code):
    core.answer(LINK, envelope(status, code))
    with pytest.raises(CoreError) as raised:
        await gateway.synonym_link(OWNER, command_id=COMMAND, card_id=CARD, other_card_id=OTHER,
                                   expected_group_version=None, expected_other_group_version=None)
    assert not isinstance(raised.value, CoreUnavailable)
    assert (raised.value.status, raised.value.code) == (status, code)


async def test_the_core_lines_of_synonym_calls_hold_no_value(core, gateway, trail_lines):
    await every_synonym_call(gateway, core)
    assert [line["route"] for line in trail_lines.events("b.core")] == [CANDIDATES, LINK, LINK_MANY, UNLINK, CLEAR]
    everything = trail_lines.everything
    for value in (str(CARD), str(OTHER), str(THIRD), str(COMMAND), "1001", "Haus", "здание"):
        assert value not in everything
