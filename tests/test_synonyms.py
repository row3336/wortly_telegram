"""The owner's synonym groups in the bot (project DEV-17 ed. 3.3 §3.5, §3.6, §4.4, §4.6; stage B4).

The real Application, router and gateway over FakeTelegram and FakeCore (MockTransport). FakeCore
keeps synonym groups with the core C4 rules the bot relies on: versions from one sequence, both
versions compared by link, the command fact by command_id (already_applied / mismatch).
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest
from telegram import Update

from conftest import feed
from fakes.core_gateway import FakeCore, core_settings, envelope, forbid_network, owner_card_wire
from fakes.telegram_api import WEB_APP_URL, callback_update, message_update, user
from wortly_telegram.app import build_application
from wortly_telegram.card_view import card_code
from wortly_telegram.gateway import SynonymList, SynonymMember, build_gateway
from wortly_telegram.messages import CALLBACK_DATA_BYTES
from wortly_telegram.router import command_id, discover
from wortly_telegram.service import card_editor, synonyms
from wortly_telegram.service.synonyms import KEYS_PER_CHAT, AddInputs, Pending, SynonymKeys
from wortly_telegram.start import TEXTS as START_TEXTS
from wortly_telegram.synonym_view import group_view, parse


OWNER_ID, OTHER_USER = 5005, 6006
OWNER = user(OWNER_ID, language_code="ru")
CARD, OTHER, THIRD, VERB = (UUID("10000000-0000-4000-8000-000000000001"), UUID("20000000-0000-4000-8000-000000000002"),
                            UUID("30000000-0000-4000-8000-000000000003"), UUID("40000000-0000-4000-8000-000000000004"))
CODE = card_code(CARD)
P = "/internal/v1/owner/synonyms/"
LINK, LINK_MANY, UNLINK, CLEAR, CANDIDATES = (P + "link", P + "link_many", P + "unlink", P + "clear",
                                              P + "candidates")
READ, SEARCH = "/internal/v1/owner/cards/read", "/internal/v1/owner/cards/search"
OWNER_ROUTES = (LINK, LINK_MANY, UNLINK, CLEAR, CANDIDATES, READ, SEARCH, "/internal/v1/owner/cards/translation",
                "/internal/v1/owner/cards/grammar")
WRITES = (LINK, LINK_MANY, UNLINK, CLEAR)
SAVED = "✅ Сохранено."
STALE = "Подтверждение устарело. Повтори действие."
CONFLICT = "Группу синонимов уже изменили. Показываю актуальные данные — повтори действие."
START = START_TEXTS["ru"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


@pytest.fixture
def core() -> FakeCore:
    core = FakeCore()
    core.summaries[str(OWNER_ID)] = {"role": "owner", "status": "admitted", "ui_language": "ru"}
    core.summaries[str(OTHER_USER)] = {"role": "user", "status": "admitted", "ui_language": "ru"}
    core.cards[str(CARD)] = owner_card_wire(str(CARD), lemma="das Haus")
    core.cards[str(OTHER)] = owner_card_wire(str(OTHER), lemma="das Gebäude", translations={"ru": ["здание"]})
    core.cards[str(THIRD)] = owner_card_wire(str(THIRD), lemma="der Bau", translations={"ru": ["здание", "стройка"]})
    core.cards[str(VERB)] = owner_card_wire(str(VERB), card_type="verb", lemma="bauen", translations={"ru": ["строить"]})
    return core


@pytest.fixture
async def start_bot(telegram, core, sleeps):
    built = []

    async def start():
        gateway = build_gateway(core_settings(), transport=core.transport())
        functions = (*card_editor.service_functions(card_editor.EditorInputs()),
                     *synonyms.service_functions(AddInputs(), SynonymKeys()))
        application = build_application(telegram.bot(), gateway=gateway, web_app_url=WEB_APP_URL,
                                        functions=functions, sleep=sleeps)
        await application.initialize()
        built.append((application, gateway))
        return application
    yield start
    for application, gateway in built:
        await application.shutdown()
        await gateway.aclose()


@pytest.fixture
async def application(start_bot):
    return await start_bot()


def press(data: str, *, callback_id: str | None = None, from_user: dict = OWNER) -> dict:
    return callback_update(data, from_user=from_user, callback_id=callback_id or uuid4().hex)


def say(text: str, *, message_id: int, from_user: dict = OWNER) -> dict:
    return message_update(text, from_user=from_user, message_id=message_id)


def owner_paths(core) -> list[str]:
    return [path for path in core.paths() if path in OWNER_ROUTES]


def writes(core) -> list[str]:
    return [path for path in core.paths() if path in WRITES]


def sent(telegram) -> list[str]:
    return [call.params.get("text") for call in telegram.calls_to("sendMessage")]


def edited(telegram) -> list[str]:
    return [call.params.get("text") for call in telegram.calls_to("editMessageText")]


def buttons(call) -> list[str]:
    if "reply_markup" not in call.params:
        return []
    return [button.get("callback_data") for row in call.json("reply_markup")["inline_keyboard"] for button in row]


def labelled(call) -> dict[str, str]:
    return {button["text"]: button["callback_data"] for row in call.json("reply_markup")["inline_keyboard"]
            for button in row}


async def add_and_find(application, telegram, *, query: str = "здание", message_id: int = 30) -> dict[str, str]:
    """➕ on the card's group view, then a text; the [Link: …] buttons by label."""
    await feed(application, press(f"sy:ad:{CODE}:n"))
    await feed(application, say(query, message_id=message_id))
    return labelled(telegram.calls_to("sendMessage")[-1])


# -- the card and the group view (§3.2, §3.5) -------------------------------------------------

async def test_the_card_lists_its_synonyms_and_opens_the_group(telegram, core, application):
    version = core.link_cards(str(CARD), str(OTHER))
    await feed(application, press(f"ce:o:{CODE}"))
    [card] = telegram.calls_to("sendMessage")
    assert card.params["text"].endswith("\nСинонимы: das Gebäude")
    assert f"sy:v:{CODE}" in buttons(card)
    await feed(application, press(f"sy:v:{CODE}"))
    [view] = telegram.calls_to("editMessageText")
    assert view.params["text"] == ("Синонимы (сущ.):\n1. das Haus — дом (эта карточка)\n"
                                   "2. das Gebäude — здание")
    assert buttons(view) == [f"sy:rm:{CODE}:{version}:1", f"sy:rm:{CODE}:{version}:2", f"sy:ad:{CODE}:n",
                             f"sy:cd:{CODE}:{version}", f"sy:cl:{CODE}:{version}", f"ce:c:{CODE}"]


async def test_a_card_without_a_group(telegram, core, application):
    await feed(application, press(f"sy:v:{CODE}"))
    [view] = telegram.calls_to("editMessageText")
    assert view.params["text"] == "Синонимы (сущ.):\nСинонимов нет."
    assert buttons(view) == [f"sy:ad:{CODE}:n", f"sy:cd:{CODE}:0", f"ce:c:{CODE}"]


# -- ➕: a read, then a button with a key -------------------------------------------------------

async def test_the_add_text_only_searches_the_type_and_writes_nothing(telegram, core, application):
    found = await add_and_find(application, telegram)
    assert sent(telegram)[0] == "Пришли слово или номер карточки того же типа."
    assert core.bodies(SEARCH) == [{"actor_telegram_id": str(OWNER_ID), "query": "здание", "limit": 10,
                                    "type": "noun"}]
    assert owner_paths(core) == [READ, SEARCH] and writes(core) == []
    # The card itself is no choice; every other one is a button, even a single one.
    assert sorted(found) == ["Связать: das Gebäude — здание · A1", "Связать: der Bau — здание · A1"]
    assert all(parse(data).action == "pk" and len(data) == len("sy:pk:") + 8 for data in found.values())
    # The same text again: buttons again, still no write.
    await feed(application, say("здание", message_id=30))
    assert len(core.bodies(SEARCH)) == 2 and writes(core) == []


async def test_one_found_card_is_still_a_button_and_nothing_found_asks_again(telegram, core, application):
    found = await add_and_find(application, telegram, query="gebäude")
    assert list(found) == ["Связать: das Gebäude — здание · A1"] and writes(core) == []
    await feed(application, say("nichts", message_id=31))
    assert sent(telegram)[-1] == "Не нашёл карточку того же типа. Пришли другое слово или номер."
    await feed(application, say("bau", message_id=32))
    assert list(labelled(telegram.calls_to("sendMessage")[-1])) == ["Связать: der Bau — здание · A1"]


async def test_members_of_the_own_group_are_no_choice(telegram, core, application):
    version = core.link_cards(str(CARD), str(OTHER))
    found = await add_and_find(application, telegram)
    assert list(found) == ["Связать: der Bau — здание · A1"]


async def test_a_link_press_takes_its_body_from_the_key_and_shows_the_merged_group(telegram, core, application):
    other_version = None  # the found card has no group yet
    found = await add_and_find(application, telegram, query="gebäude")
    update = press(found["Связать: das Gebäude — здание · A1"], callback_id="pk-1")
    await feed(application, update)
    assert core.bodies(LINK) == [{
        "actor_telegram_id": str(OWNER_ID), "command_id": str(command_id(Update.de_json(update, application.bot))),
        "card_id": str(CARD), "other_card_id": str(OTHER), "expected_group_version": None,
        "expected_other_group_version": other_version}]
    # No read between the press and the write (§4.4).
    assert owner_paths(core) == [READ, SEARCH, LINK]
    [answer] = telegram.calls_to("editMessageText")
    assert answer.params["text"].startswith(f"{SAVED}\n\nСинонимы (сущ.):\n1. das Haus — дом (эта карточка)\n"
                                            "2. das Gebäude — здание")


async def test_link_merges_a_group_with_both_versions_from_the_key(telegram, core, application):
    other_version = core.link_cards(str(OTHER), str(THIRD))
    found = await add_and_find(application, telegram, query="gebäude")
    await feed(application, press(found["Связать: das Gebäude — здание · A1"]))
    [body] = core.bodies(LINK)
    assert (body["expected_group_version"], body["expected_other_group_version"]) == (None, str(other_version))
    assert "3. der Bau — здание" in edited(telegram)[-1]


async def test_the_same_link_press_twice_around_a_merge_is_the_same_body(telegram, core, application):
    found = await add_and_find(application, telegram, query="gebäude")
    update = press(found["Связать: das Gebäude — здание · A1"], callback_id="pk-twice")
    await feed(application, update)
    # Between the deliveries the groups change (a merge made from another chat).
    core.link_cards(str(CARD), str(THIRD))
    await feed(application, update)
    first, second = core.bodies(LINK)
    assert first == second
    saved, repeated = edited(telegram)
    assert saved.startswith(SAVED) and "Сохранено" not in repeated
    assert "der Bau" in repeated


async def test_a_new_press_on_a_stale_link_is_a_conflict(telegram, core, application):
    found = await add_and_find(application, telegram, query="gebäude")
    core.link_cards(str(OTHER), str(THIRD))
    await feed(application, press(found["Связать: das Gebäude — здание · A1"]))
    assert owner_paths(core)[-2:] == [LINK, READ]
    [answer] = edited(telegram)
    assert answer == f"{CONFLICT}\n\nСинонимы (сущ.):\nСинонимов нет."


async def test_a_key_after_a_restart_is_out_of_date_and_writes_nothing(telegram, core, start_bot):
    first = await start_bot()
    found = await add_and_find(first, telegram, query="gebäude")
    restarted = await start_bot()
    await feed(restarted, press(found["Связать: das Gebäude — здание · A1"]))
    assert sent(telegram)[-1] == STALE and writes(core) == []


async def test_a_text_after_a_restart_gets_start(telegram, core, start_bot):
    first = await start_bot()
    await feed(first, press(f"sy:ad:{CODE}:n"))
    restarted = await start_bot()
    await feed(restarted, say("здание", message_id=40))
    assert sent(telegram)[-1] == START and owner_paths(core) == []


def test_a_chat_keeps_its_last_20_keys():
    keys = SynonymKeys()
    made = [keys.put(1, Pending("clear", CARD, number)) for number in range(1, KEYS_PER_CHAT + 2)]
    assert keys.get(1, made[0]) is None
    assert keys.get(1, made[1]) == Pending("clear", CARD, 2) and keys.get(1, made[-1]) == Pending("clear", CARD, 21)
    assert keys.get(2, made[-1]) is None
    assert all(len(key) == 8 for key in made) and len(set(made)) == len(made)


# -- pick by translation and clear (with a confirmation, BE-7) --------------------------------

async def test_pick_by_translation_confirms_and_links_the_candidates_from_the_key(telegram, core, application):
    await feed(application, press(f"sy:cd:{CODE}:0"))
    assert owner_paths(core) == [CANDIDATES] and writes(core) == []
    [preview] = telegram.calls_to("sendMessage")
    assert preview.params["text"] == ("Подобрано по общему переводу в языке словаря. Связать все?\n"
                                      "1. das Gebäude — здание\n2. der Bau — здание")
    confirm, cancel = buttons(preview)
    assert parse(confirm).action == "ok" and cancel == f"sy:v:{CODE}"
    update = press(confirm, callback_id="ok-1")
    await feed(application, update)
    core.link_cards(str(OTHER), str(THIRD))  # the group changes in between: a new version
    await feed(application, update)
    first, second = core.bodies(LINK_MANY)
    assert first == second == {"actor_telegram_id": str(OWNER_ID),
                               "command_id": str(command_id(Update.de_json(update, application.bot))),
                               "card_id": str(CARD), "other_card_ids": [str(OTHER), str(THIRD)],
                               "expected_group_version": None}
    assert edited(telegram)[0].startswith(SAVED) and "Сохранено" not in edited(telegram)[1]


async def test_no_candidates(telegram, core, application):
    await feed(application, press(f"sy:cd:{card_code(VERB)}:0"))
    assert sent(telegram) == ["Карточек того же типа с таким же переводом в языке словаря нет."]


async def test_clear_asks_and_dissolves_the_group(telegram, core, application):
    version = core.link_cards(str(CARD), str(OTHER), str(THIRD))
    await feed(application, press(f"sy:cl:{CODE}:{version}"))
    assert writes(core) == []
    [question] = telegram.calls_to("sendMessage")
    assert question.params["text"] == "Очистить группу? Все карточки перестанут быть синонимами друг друга."
    confirm, cancel = buttons(question)
    await feed(application, press(cancel))
    assert writes(core) == []
    await feed(application, press(confirm))
    assert core.bodies(CLEAR)[0]["expected_group_version"] == str(version)
    assert edited(telegram)[-1] == f"{SAVED}\n\nСинонимы (сущ.):\nСинонимов нет."


async def test_a_key_of_another_kind_is_out_of_date(telegram, core, application):
    version = core.link_cards(str(CARD), str(OTHER))
    await feed(application, press(f"sy:cl:{CODE}:{version}"))
    confirm = buttons(telegram.calls_to("sendMessage")[-1])[0]
    await feed(application, press(confirm.replace("sy:ok:", "sy:pk:")))
    assert sent(telegram)[-1] == STALE and writes(core) == []


# -- ✖ i -------------------------------------------------------------------------------------

async def test_remove_carries_card_version_and_index_and_works_after_a_restart(telegram, core, start_bot):
    version = core.link_cards(str(CARD), str(OTHER), str(THIRD))
    await start_bot()
    restarted = await start_bot()
    update = press(f"sy:rm:{CODE}:{version}:2", callback_id="rm-1")
    await feed(restarted, update)
    assert core.bodies(UNLINK) == [{"actor_telegram_id": str(OWNER_ID),
                                    "command_id": str(command_id(Update.de_json(update, restarted.bot))),
                                    "card_id": str(CARD), "index": 2, "expected_group_version": str(version)}]
    assert owner_paths(core) == [UNLINK]
    assert edited(telegram)[-1].startswith(f"{SAVED}\n\nСинонимы (сущ.):\n1. das Haus — дом (эта карточка)\n"
                                           "2. der Bau — здание")


@pytest.mark.parametrize(("answer", "text"), [
    (envelope(422, "VALIDATION_FAILED"), "Ядро не приняло изменение: синонимами бывают только опубликованные "
                                         "карточки одного типа. Ничего не изменено."),
    (envelope(403, "FORBIDDEN"), "Это действие доступно только владельцу."),
    (envelope(409, "WRITE_UNAVAILABLE"), "Карточка недоступна. Обнови поиск: /find"),
    (httpx.Response(503), "Ядро не ответило. Изменение могло сохраниться — нажми «Обновить», прежде чем повторять."),
])
async def test_the_refusals_of_a_synonym_command(telegram, core, application, answer, text):
    core.answer(UNLINK, answer)
    await feed(application, press(f"sy:rm:{CODE}:5:1"))
    assert sent(telegram) == [text] and telegram.calls_to("editMessageText") == []


async def test_a_payload_mismatch_is_answered_like_a_conflict(telegram, core, application):
    core.answer(UNLINK, envelope(409, "COMMAND_PAYLOAD_MISMATCH"))
    await feed(application, press(f"sy:rm:{CODE}:5:1"))
    assert edited(telegram) == [f"{CONFLICT}\n\nСинонимы (сущ.):\nСинонимов нет."]


async def test_a_repeat_after_a_demotion_shows_nothing_and_does_not_fail(telegram, core, application, trail_lines):
    async def answer(request):
        body = json.loads(request.content)
        fact = {"id": body["command_id"], "status": "already_applied", "applied_at": "2026-10-15T12:00:00Z",
                "target_id": str(CARD), "object_version": "8"}
        return httpx.Response(200, json={"command": fact, "current": None})
    core.answer(UNLINK, answer)
    await feed(application, press(f"sy:rm:{CODE}:5:1"))
    assert sent(telegram) == [] and edited(telegram) == []
    assert [line["outcome"] for line in trail_lines.events("b.update")] == ["handled"]


# -- buttons, audience, trail ---------------------------------------------------------------

def test_remove_buttons_fit_64_bytes_and_stop_at_99_members():
    members = tuple(SynonymMember(UUID(int=number + 1), f"w{number}", "noun", None, "visible", None)
                    for number in range(120))
    view = group_view(CARD, "noun", SynonymList(9_223_372_036_854_775_807, members), "ru")
    data = [button.data for row in view.buttons for button in row]
    removes = [item for item in data if item.startswith("sy:rm:")]
    assert len(removes) == 99 and removes[-1] == f"sy:rm:{CODE}:9223372036854775807:99"
    assert max(len(item.encode("utf-8")) for item in data) == 51 <= CALLBACK_DATA_BYTES
    assert all(parse(item) is not None for item in data if item.startswith("sy:"))


async def test_a_malformed_synonym_button_is_only_acknowledged(telegram, core, application):
    for data in ("sy:x", f"sy:v:{CODE}x", f"sy:rm:{CODE}:0:1", f"sy:rm:{CODE}:5:0", f"sy:rm:{CODE}:5:100",
                 f"sy:cl:{CODE}:0", f"sy:ad:{CODE}:z", f"sy:ad:{CODE}:0:n", "sy:pk:short", "sy:ok:toolongkey",
                 f"sy:v:{CODE}:{OWNER_ID}"):
        await feed(application, press(data))
    assert owner_paths(core) == [] and sent(telegram) == [] and edited(telegram) == []
    assert len(telegram.calls_to("answerCallbackQuery")) == 11


@pytest.mark.parametrize("update", [
    lambda: press(f"sy:v:{CODE}", from_user=user(OTHER_USER, language_code="ru")),
    lambda: press(f"sy:rm:{CODE}:5:1", from_user=user(7007, language_code="ru")),
])
async def test_anyone_but_the_owner_gets_the_default_answer(telegram, core, application, update):
    await feed(application, update())
    assert owner_paths(core) == [] and sent(telegram) == [START]


async def test_no_message_of_the_synonyms_has_a_parse_mode(telegram, core, application):
    found = await add_and_find(application, telegram, query="gebäude")
    await feed(application, press(found["Связать: das Gebäude — здание · A1"]))
    await feed(application, press(f"sy:cd:{CODE}:{core._version_of(str(CARD))}"))
    assert not [call for call in telegram.calls if "parse_mode" in call.params]


async def test_the_trail_names_no_chat_person_text_or_key(telegram, core, application, trail_lines):
    await feed(application, press(f"sy:ad:{CODE}:n"))
    await feed(application, say("gebäude-query", message_id=30))
    await feed(application, say("gebäude", message_id=31))
    found = labelled(telegram.calls_to("sendMessage")[-1])
    key = found["Связать: das Gebäude — здание · A1"]
    await feed(application, press(key))
    updates = trail_lines.events("b.update")
    assert {line["fn"] for line in updates} == {"synonyms"}
    assert {line["route"] for line in trail_lines.events("b.core")} >= {SEARCH, LINK}
    everything = trail_lines.everything
    for value in (str(OWNER_ID), "Anna", "gebäude", "Gebäude", "das Haus", str(CARD), CODE, key, key[6:], "sy:"):
        assert value not in everything, value


def test_the_shipped_service_package_declares_the_synonyms():
    [function] = [item for item in discover() if item.name == "synonyms"]
    assert (function.commands, function.callback_prefix, function.audience) == ((), "sy", "owner")
    assert function.text_input is not None and synonyms.SERVICE_FUNCTIONS == (function,)


# -- fix1 (QA B1b/B4): versions at the search, exact answers, distinct buttons -------------------

async def test_the_own_group_version_is_the_one_at_the_search(telegram, core, application):
    # §3.5: both versions "at the moment of the search", not at the ➕ press.
    await feed(application, press(f"sy:ad:{CODE}:n"))
    version = core.link_cards(str(CARD), str(THIRD))  # changed between ➕ and the text
    await feed(application, say("gebäude", message_id=50))
    found = labelled(telegram.calls_to("sendMessage")[-1])
    await feed(application, press(found["Связать: das Gebäude — здание · A1"]))
    [body] = core.bodies(LINK)
    assert (body["expected_group_version"], body["expected_other_group_version"]) == (str(version), None)
    assert edited(telegram)[-1].startswith(SAVED)


@pytest.mark.parametrize("query", ["gebäude", "haus"])
async def test_only_the_card_or_its_group_found_says_so(telegram, core, application, query):
    core.link_cards(str(CARD), str(OTHER))
    await feed(application, press(f"sy:ad:{CODE}:n"))
    await feed(application, say(query, message_id=51))
    assert sent(telegram)[-1] == ("Нашлась только эта карточка или карточки, уже связанные с ней. "
                                  "Пришли другое слово или номер.")
    assert writes(core) == []


async def test_the_same_clear_confirmation_twice_is_the_same_command(telegram, core, application):
    version = core.link_cards(str(CARD), str(OTHER))
    await feed(application, press(f"sy:cl:{CODE}:{version}"))
    confirm = buttons(telegram.calls_to("sendMessage")[-1])[0]
    update = press(confirm, callback_id="ok-clear")
    await feed(application, update)
    await feed(application, update)
    first, second = core.bodies(CLEAR)
    assert first == second == {"actor_telegram_id": str(OWNER_ID),
                               "command_id": str(command_id(Update.de_json(update, application.bot))),
                               "card_id": str(CARD), "expected_group_version": str(version)}
    assert edited(telegram)[0].startswith(SAVED) and "Сохранено" not in edited(telegram)[1]


async def test_pick_by_translation_keeps_the_version_of_an_existing_group(telegram, core, application):
    version = core.link_cards(str(CARD), str(OTHER))
    await feed(application, press(f"sy:cd:{CODE}:{version}"))
    confirm = buttons(telegram.calls_to("sendMessage")[-1])[0]
    await feed(application, press(confirm))
    [body] = core.bodies(LINK_MANY)
    assert (body["other_card_ids"], body["expected_group_version"]) == ([str(THIRD)], str(version))
    assert edited(telegram)[-1].startswith(SAVED)


async def test_more_found_cards_ask_to_refine(telegram, core, application):
    for number in range(12):
        card_id = str(uuid4())
        core.cards[card_id] = owner_card_wire(card_id, lemma=f"Gebäude{number}", translations={"ru": ["здание"]})
    await feed(application, press(f"sy:ad:{CODE}:n"))
    await feed(application, say("gebäude", message_id=52))
    answer = telegram.calls_to("sendMessage")[-1]
    assert answer.params["text"] == "Выбери карточку, которую связать:\nПоказаны первые 10 найденных, уточни запрос."
    assert len(buttons(answer)) == 10


async def test_cards_with_one_lemma_get_distinct_buttons(telegram, core, application):
    twins = [str(UUID(int=900 + number)) for number in range(3)]
    for card_id, ru in zip(twins, ("дом", "дом", "жилище")):
        core.cards[card_id] = owner_card_wire(card_id, lemma="das Heim", translations={"ru": [ru]})
    await feed(application, press(f"sy:ad:{CODE}:n"))
    await feed(application, say("heim", message_id=53))
    found = labelled(telegram.calls_to("sendMessage")[-1])
    assert sorted(found) == ["Связать: das Heim — дом · A1 (1)", "Связать: das Heim — дом · A1 (2)",
                             "Связать: das Heim — жилище · A1"]
    assert len(set(found.values())) == 3
    assert all(len(data.encode("utf-8")) <= CALLBACK_DATA_BYTES for data in found.values())
