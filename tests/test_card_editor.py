"""The owner's common editor in the bot (project DEV-17 ed. 3.3 §3, §4.4, §4.6; stages B2, B3).

Updates go through the real Application, router and gateway; the Bot API is FakeTelegram
and the core's internal API is FakeCore behind httpx.MockTransport (no socket), with the
core's rules the bot relies on: the owner check, the command fact by command_id
(already_applied / COMMAND_PAYLOAD_MISMATCH), the revision check and the list operations.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from telegram import Update

from conftest import feed
from fakes.core_gateway import FakeCore, core_settings, envelope, forbid_network, owner_card_wire
from fakes.telegram_api import WEB_APP_URL, callback_update, message_update, user
from wortly_telegram.app import build_application
from wortly_telegram.card_view import (
    CALLBACK_PREFIX, GRAMMAR_FIELDS, TEXT_MAX_CHARACTERS, View, card_code, card_of, field_options, grammar_view,
    list_view, parse, with_notice,
)
from wortly_telegram.gateway import GrammarForm, OwnerCard, build_gateway
from wortly_telegram.messages import CALLBACK_DATA_BYTES
from wortly_telegram.router import command_id, discover
from wortly_telegram.service import card_editor
from wortly_telegram.service.card_editor import EditorInputs, service_functions
from wortly_telegram.start import TEXTS as START_TEXTS


OWNER_ID, OTHER_ID = 5005, 6006
OWNER, OTHER = user(OWNER_ID, language_code="ru"), user(OTHER_ID, language_code="ru")
CARD = UUID("6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5")
CODE = card_code(CARD)
SEARCH, READ, TRANSLATION, GRAMMAR = ("/internal/v1/owner/cards/search", "/internal/v1/owner/cards/read",
                                      "/internal/v1/owner/cards/translation", "/internal/v1/owner/cards/grammar")
OWNER_ROUTES = (SEARCH, READ, TRANSLATION, GRAMMAR)
SAVED, CONFLICT = "✅ Сохранено.", "Карточку уже изменили (возможно, в Mini App). Показываю актуальные данные — повтори действие."
START = START_TEXTS["ru"]
CORE_SRC = Path(__file__).resolve().parents[2] / "core" / "src"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


@pytest.fixture
def core() -> FakeCore:
    core = FakeCore()
    core.summaries[str(OWNER_ID)] = {"role": "owner", "status": "admitted", "ui_language": "ru"}
    core.summaries[str(OTHER_ID)] = {"role": "user", "status": "admitted", "ui_language": "ru"}
    core.cards[str(CARD)] = owner_card_wire(str(CARD))
    return core


@pytest.fixture
def inputs() -> EditorInputs:
    return EditorInputs()


@pytest.fixture
async def start_bot(telegram, core, sleeps):
    built = []

    async def start(inputs: EditorInputs):
        gateway = build_gateway(core_settings(), transport=core.transport())
        application = build_application(telegram.bot(), gateway=gateway, web_app_url=WEB_APP_URL,
                                        functions=service_functions(inputs), sleep=sleeps)
        await application.initialize()
        built.append((application, gateway))
        return application
    yield start
    for application, gateway in built:
        await application.shutdown()
        await gateway.aclose()


@pytest.fixture
async def application(start_bot, inputs):
    return await start_bot(inputs)


def press(data: str, *, callback_id: str | None = None, from_user: dict = OWNER, **extra) -> dict:
    return callback_update(data, from_user=from_user, callback_id=callback_id or uuid4().hex, **extra)


def say(text: str, *, message_id: int, from_user: dict = OWNER) -> dict:
    return message_update(text, from_user=from_user, message_id=message_id)


def owner_paths(core) -> list[str]:
    return [path for path in core.paths() if path in OWNER_ROUTES]


def sent(telegram) -> list[str]:
    return [call.params.get("text") for call in telegram.calls_to("sendMessage")]


def edited(telegram) -> list[str]:
    return [call.params.get("text") for call in telegram.calls_to("editMessageText")]


def buttons(call) -> list[str]:
    if "reply_markup" not in call.params:
        return []
    return [button.get("callback_data") for row in call.json("reply_markup")["inline_keyboard"] for button in row]


def wire_card(**changes) -> OwnerCard:
    values = dict(card_id=CARD, card_type="noun", lemma="das Haus", cefr="A1", revision=7, language="ru",
                  state="visible", translations={"ru": ("дом", "здание"), "uk": ("будинок",), "en": ()},
                  grammar={"gender": GrammarForm("applicable", "neuter"), "singular": GrammarForm("applicable", "Haus"),
                           "plural": GrammarForm("applicable", "Häuser"), "article": GrammarForm("applicable")})
    return OwnerCard(**(values | changes))


# -- /find (§3.1) --------------------------------------------------------------------------

async def test_find_with_a_word_lists_the_cards_as_buttons(telegram, core, application):
    hidden = uuid4()
    core.cards[str(hidden)] = owner_card_wire(str(hidden), lemma="das Hausdach", translations={"uk": ["дах"]},
                                              cefr=None)
    await feed(application, say("/find haus", message_id=10))
    assert core.bodies(SEARCH) == [{"actor_telegram_id": str(OWNER_ID), "query": "haus", "limit": 10}]
    [answer] = telegram.calls_to("sendMessage")
    assert answer.params["text"] == "Найдено карточек: 2."
    labels = {button["text"]: button["callback_data"]
              for row in answer.json("reply_markup")["inline_keyboard"] for button in row}
    assert labels == {"das Haus · сущ. · A1": f"ce:o:{CODE}", "das Hausdach · сущ. · скрыта": f"ce:o:{card_code(hidden)}"}
    assert "parse_mode" not in answer.params


async def test_find_without_a_word_asks_and_the_next_text_is_the_query(telegram, core, application):
    await feed(application, say("/find", message_id=10))
    assert sent(telegram) == ["Пришли слово или номер карточки."]
    await feed(application, say("nichts", message_id=11))
    assert sent(telegram)[-1] == "Не нашёл. Пришли другое слово или номер."
    # "Send another word": the input still waits, and the next text searches again.
    await feed(application, say(str(CARD), message_id=12))
    assert [body["query"] for body in core.bodies(SEARCH)] == ["nichts", str(CARD)]
    assert buttons(telegram.calls_to("sendMessage")[-1]) == [f"ce:o:{CODE}"]
    # Found: the input ended, a further text is no query.
    await feed(application, say("haus", message_id=13))
    assert len(core.bodies(SEARCH)) == 2 and sent(telegram)[-1] == START


async def test_a_blank_query_and_a_query_the_core_refuses_ask_again(telegram, core, application):
    await feed(application, say("/find", message_id=10))
    await feed(application, say("   ", message_id=11))
    # The blank text is answered with the prompt again and reaches no core route.
    assert owner_paths(core) == [] and sent(telegram) == ["Пришли слово или номер карточки."] * 2
    core.answer(SEARCH, envelope(422, "VALIDATION_FAILED"))
    await feed(application, say("Haus", message_id=12))
    assert sent(telegram)[-1] == "Не нашёл. Пришли другое слово или номер."
    await feed(application, say("Haus", message_id=13))
    assert len(core.bodies(SEARCH)) == 2 and buttons(telegram.calls_to("sendMessage")[-1]) == [f"ce:o:{CODE}"]


async def test_more_than_ten_cards_ask_to_refine(telegram, core, application):
    for number in range(11):
        card_id = str(uuid4())
        core.cards[card_id] = owner_card_wire(card_id, lemma=f"Haus{number}")
    await feed(application, say("/find haus", message_id=10))
    [answer] = telegram.calls_to("sendMessage")
    assert answer.params["text"] == "Показаны первые 10, уточни запрос."
    assert len(buttons(answer)) == 10


async def test_a_query_over_200_characters_is_not_sent(telegram, core, application):
    await feed(application, say("/find " + "x" * 201, message_id=10))
    assert owner_paths(core) == []
    assert sent(telegram) == ["Запрос длиннее 200 символов. Пришли слово короче."]


@pytest.mark.parametrize(("answer", "text"), [
    (envelope(403, "FORBIDDEN"), "Это действие доступно только владельцу."),
    (httpx.Response(500), "Ядро не ответило. Попробуй ещё раз."),
])
async def test_a_refused_search_is_answered(telegram, core, application, answer, text):
    core.answer(SEARCH, answer)
    await feed(application, say("/find haus", message_id=10))
    assert sent(telegram) == [text]


# -- the card (§3.2) -----------------------------------------------------------------------

async def test_a_found_card_opens_with_its_state_translations_and_buttons(telegram, core, application):
    await feed(application, press(f"ce:o:{CODE}"))
    assert core.bodies(READ) == [{"actor_telegram_id": str(OWNER_ID), "card_id": str(CARD)}]
    [answer] = telegram.calls_to("sendMessage")
    assert answer.params["text"] == ("das Haus — сущ., A1\n"
                                     "Состояние: опубликована\n"
                                     "Переводы ru (язык словаря): 1. дом; 2. здание\n"
                                     "Другие языки свёрнуты: uk (1), en (0)\n"
                                     "Грамматика: род — das; ед. ч. — Haus; мн. ч. — Häuser; артикль — Применим")
    # R17-02 variant 1: every language has its list; the other ones stay folded until opened.
    assert buttons(answer) == [f"ce:tl:ru:{CODE}", f"ce:tl:uk:{CODE}", f"ce:tl:en:{CODE}", f"ce:g:{CODE}",
                               f"ce:c:{CODE}"]
    assert "будинок" not in answer.params["text"]
    assert [call.params.get("text") for call in telegram.calls_to("answerCallbackQuery")] == [None]


async def test_a_hidden_card_says_it_and_a_phrase_has_no_grammar(telegram, core, application):
    core.cards[str(CARD)] = owner_card_wire(str(CARD), card_type="phrase", lemma="zu Hause",
                                            translations={"uk": ["вдома"]})
    await feed(application, press(f"ce:c:{CODE}"))
    [answer] = telegram.calls_to("editMessageText")
    assert "Состояние: скрыта — нет общего перевода (ru)" in answer.params["text"]
    assert "Переводы ru (язык словаря): —" in answer.params["text"]
    assert f"ce:g:{CODE}" not in buttons(answer)


@pytest.mark.parametrize(("answer", "text"), [
    (envelope(409, "WRITE_UNAVAILABLE"), "Карточка недоступна. Обнови поиск: /find"),
    (envelope(403, "FORBIDDEN"), "Это действие доступно только владельцу."),
    (httpx.ConnectError("down"), "Ядро не ответило. Попробуй ещё раз."),
])
async def test_a_refused_read_is_answered(telegram, core, application, answer, text):
    core.answer(READ, answer)
    await feed(application, press(f"ce:c:{CODE}"))
    assert sent(telegram) == [text]
    assert telegram.calls_to("editMessageText") == []


# -- the translations of one language (§3.3) ------------------------------------------------

async def test_the_list_carries_language_revision_number_and_count(telegram, application):
    await feed(application, press(f"ce:tl:ru:{CODE}"))
    [answer] = telegram.calls_to("editMessageText")
    assert answer.params["text"] == "das Haus — переводы ru:\n1. дом\n2. здание"
    assert buttons(answer) == [f"ce:ed:ru:{CODE}:7:1:2", f"ce:dn:ru:{CODE}:7:1:2", f"ce:rm:ru:{CODE}:7:1:2",
                               f"ce:ed:ru:{CODE}:7:2:2", f"ce:up:ru:{CODE}:7:2:2", f"ce:rm:ru:{CODE}:7:2:2",
                               f"ce:ad:ru:{CODE}:7:2", f"ce:c:{CODE}"]


async def test_another_language_is_edited_like_the_dictionary_language(telegram, core, application):
    # R17-02 variant 1 (15.10.2026); the core accepts it once the language gate is lifted there.
    await feed(application, press(f"ce:tl:uk:{CODE}"))
    assert buttons(telegram.calls_to("editMessageText")[0])[0] == f"ce:ed:uk:{CODE}:7:1:1"
    await feed(application, press(f"ce:ad:uk:{CODE}:7:1"))
    await feed(application, say("хата", message_id=20))
    [body] = core.bodies(TRANSLATION)
    assert (body["language"], body["op"], body["text"]) == ("uk", "append", "хата")
    assert core.cards[str(CARD)]["translations"]["uk"] == ["будинок", "хата"]


async def test_a_list_button_writes_at_once_and_the_list_is_redrawn_with_saved(telegram, core, application):
    update = press(f"ce:up:ru:{CODE}:7:2:2", callback_id="up-1")
    await feed(application, update)
    assert core.bodies(TRANSLATION) == [{
        "actor_telegram_id": str(OWNER_ID), "command_id": str(command_id(Update.de_json(update, application.bot))),
        "card_id": str(CARD), "language": "ru", "op": "move_up", "index": 2, "expected_card_revision": "7"}]
    # No read of the card between the update and the write (§4.4).
    assert owner_paths(core) == [TRANSLATION]
    [answer] = telegram.calls_to("editMessageText")
    assert answer.params["text"] == f"{SAVED}\n\ndas Haus — переводы ru:\n1. здание\n2. дом"
    assert f"ce:dn:ru:{CODE}:8:1:2" in buttons(answer)


async def test_the_same_press_twice_is_the_same_body_and_already_applied_redraws_without_saved(
        telegram, core, application):
    update = press(f"ce:rm:ru:{CODE}:7:1:2", callback_id="rm-1")
    await feed(application, update)
    # Between the two deliveries the card changes elsewhere (the Mini App, another command).
    core.cards[str(CARD)] = owner_card_wire(str(CARD), revision=30, translations={"ru": ["здание", "хоромы"]})
    await feed(application, update)
    first, second = core.bodies(TRANSLATION)
    assert first == second
    assert owner_paths(core) == [TRANSLATION, TRANSLATION]
    saved, repeated = edited(telegram)
    assert saved.startswith(SAVED)
    assert "Сохранено" not in repeated and repeated.startswith("das Haus — переводы ru:")


async def test_a_new_press_on_a_stale_list_is_a_conflict_and_the_list_is_redrawn(telegram, core, application):
    core.cards[str(CARD)]["card_revision"] = "9"
    await feed(application, press(f"ce:dn:ru:{CODE}:7:1:2"))
    assert owner_paths(core) == [TRANSLATION, READ]
    [answer] = telegram.calls_to("editMessageText")
    assert answer.params["text"] == f"{CONFLICT}\n\ndas Haus — переводы ru:\n1. дом\n2. здание"
    assert f"ce:dn:ru:{CODE}:9:1:2" in buttons(answer)


async def test_a_payload_mismatch_is_answered_like_a_conflict(telegram, core, application):
    core.answer(TRANSLATION, envelope(409, "COMMAND_PAYLOAD_MISMATCH"))
    await feed(application, press(f"ce:dn:ru:{CODE}:7:1:2"))
    assert edited(telegram)[0].startswith(CONFLICT)


async def test_removing_the_only_translation_is_refused_without_the_core(telegram, core, application):
    await feed(application, press(f"ce:rm:uk:{CODE}:7:1:1"))
    assert owner_paths(core) == []
    assert sent(telegram) == ["Нельзя убрать последний общий перевод языка: без него карточка пропадёт из словаря "
                              "у всех. Сначала добавь другой перевод. Убрать карточку можно только снятием "
                              "с публикации."]


@pytest.mark.parametrize(("answer", "text"), [
    (envelope(422, "VALIDATION_FAILED"), "Ядро не приняло значение: карточка стала бы неполной или письменный "
                                         "вопрос по ней — невозможным. Ничего не изменено."),
    (envelope(403, "FORBIDDEN"), "Это действие доступно только владельцу."),
    (envelope(409, "WRITE_UNAVAILABLE"), "Карточка недоступна. Обнови поиск: /find"),
    (httpx.Response(503, json={"error": {"code": "WRITE_UNAVAILABLE"}}),
     "Ядро не ответило. Изменение могло сохраниться — нажми «Обновить», прежде чем повторять."),
    (httpx.ReadTimeout("slow"),
     "Ядро не ответило. Изменение могло сохраниться — нажми «Обновить», прежде чем повторять."),
])
async def test_the_refusals_of_a_command_are_answered(telegram, core, application, answer, text):
    core.answer(TRANSLATION, answer)
    await feed(application, press(f"ce:dn:ru:{CODE}:7:1:2"))
    assert sent(telegram) == [text]
    assert telegram.calls_to("editMessageText") == []


async def test_a_repeat_after_a_demotion_shows_no_card(telegram, core, application, trail_lines):
    fact = {"id": str(uuid4()), "status": "already_applied", "applied_at": "2026-10-15T12:00:00Z",
            "target_id": str(CARD), "object_version": "8"}

    async def answer(request):
        import json
        return httpx.Response(200, json={"command": {**fact, "id": json.loads(request.content)["command_id"]},
                                         "current": None})
    core.answer(TRANSLATION, answer)
    await feed(application, press(f"ce:dn:ru:{CODE}:7:1:2"))
    assert telegram.calls_to("editMessageText") == [] and telegram.calls_to("sendMessage") == []
    # Silence by design, not a failed handler (b.update outcome failed).
    assert [line["outcome"] for line in trail_lines.events("b.update")] == ["handled"]
    assert len(telegram.calls_to("answerCallbackQuery")) == 1


async def test_edit_shows_the_echo_and_the_prompt_then_the_text_replaces_it(telegram, core, application):
    await feed(application, press(f"ce:ed:ru:{CODE}:7:2:2"))
    assert owner_paths(core) == [READ]
    echo, prompt = telegram.calls_to("sendMessage")
    assert echo.params["parse_mode"] == "MarkdownV2"
    assert echo.params["text"] == "Текущее значение\n\n`здание`\n\nНажми на значение, чтобы скопировать\\."
    assert prompt.params["text"] == "Пришли новый текст перевода №2." and "parse_mode" not in prompt.params
    message = say("постройка, корпус", message_id=40)
    await feed(application, message)
    [body] = core.bodies(TRANSLATION)
    assert (body["op"], body["index"], body["text"], body["expected_card_revision"]) == (
        "replace", 2, "постройка, корпус", "7")
    assert body["command_id"] == str(command_id(Update.de_json(message, application.bot)))
    assert owner_paths(core) == [READ, TRANSLATION]
    assert sent(telegram)[-1] == f"{SAVED}\n\ndas Haus — переводы ru:\n1. дом\n2. постройка, корпус"


async def test_the_same_text_message_twice_is_the_same_command_and_another_text_gets_start(
        telegram, core, application):
    await feed(application, press(f"ce:ed:ru:{CODE}:7:1:2"))
    message = say("домик", message_id=41)
    await feed(application, message)
    core.cards[str(CARD)] = owner_card_wire(str(CARD), revision=12)
    await feed(application, message)
    first, second = core.bodies(TRANSLATION)
    assert first == second
    assert "Сохранено" not in sent(telegram)[-1]
    # Any other text: the input has ended, the default answer and no write.
    await feed(application, say("ещё", message_id=42))
    assert sent(telegram)[-1] == START
    assert len(core.bodies(TRANSLATION)) == 2


async def test_a_stale_edit_button_starts_no_input(telegram, core, application):
    core.cards[str(CARD)]["card_revision"] = "8"
    await feed(application, press(f"ce:ed:ru:{CODE}:7:1:2"))
    assert edited(telegram) == [f"{CONFLICT}\n\ndas Haus — переводы ru:\n1. дом\n2. здание"]
    await feed(application, say("домик", message_id=43))
    assert owner_paths(core) == [READ] and sent(telegram)[-1] == START


async def test_add_takes_one_line_and_keeps_the_commas(telegram, core, application):
    await feed(application, press(f"ce:ad:ru:{CODE}:7:2"))
    assert sent(telegram) == ["Пришли новый перевод одной строкой."]
    await feed(application, say("жилище\nкров", message_id=50))
    assert owner_paths(core) == [] and sent(telegram)[-1] == "Пришли один перевод одной строкой."
    await feed(application, say("жильё, кров", message_id=51))
    [body] = core.bodies(TRANSLATION)
    assert (body["op"], body["text"]) == ("append", "жильё, кров") and "index" not in body
    assert core.cards[str(CARD)]["translations"]["ru"] == ["дом", "здание", "жильё, кров"]


async def test_a_hidden_card_gets_its_first_translation(telegram, core, application):
    core.cards[str(CARD)] = owner_card_wire(str(CARD), translations={"uk": ["будинок"]})
    await feed(application, press(f"ce:tl:ru:{CODE}"))
    assert buttons(telegram.calls_to("editMessageText")[0]) == [f"ce:ad:ru:{CODE}:7:0", f"ce:c:{CODE}"]
    await feed(application, press(f"ce:ad:ru:{CODE}:7:0"))
    await feed(application, say("дом", message_id=52))
    assert core.cards[str(CARD)]["state"] == "visible"


async def test_a_validation_refusal_keeps_the_input(telegram, core, application):
    core.answer(TRANSLATION, envelope(422, "VALIDATION_FAILED"))
    await feed(application, press(f"ce:ad:ru:{CODE}:7:2"))
    await feed(application, say("x", message_id=53))
    await feed(application, say("y", message_id=54))
    assert [body["text"] for body in core.bodies(TRANSLATION)] == ["x", "y"]


async def test_a_button_ends_the_input(telegram, core, application):
    await feed(application, press(f"ce:ad:ru:{CODE}:7:2"))
    await feed(application, press(f"ce:c:{CODE}"))
    await feed(application, say("дом", message_id=55))
    assert core.bodies(TRANSLATION) == [] and sent(telegram)[-1] == START


# -- text without an input, restarts, other users (§3.6, ВП-27) ------------------------------

async def test_a_text_without_an_input_gets_start_and_writes_nothing(telegram, core, application):
    await feed(application, say("das Haus", message_id=60))
    assert sent(telegram) == [START]
    assert owner_paths(core) == []


async def test_a_text_after_a_restart_gets_start_and_writes_nothing(telegram, core, start_bot):
    first = await start_bot(EditorInputs())
    await feed(first, press(f"ce:ad:ru:{CODE}:7:2"))
    restarted = await start_bot(EditorInputs())
    await feed(restarted, say("жильё", message_id=61))
    assert sent(telegram)[-1] == START
    assert core.bodies(TRANSLATION) == []


async def test_an_old_button_works_after_a_restart(telegram, core, start_bot):
    restarted = await start_bot(EditorInputs())
    await feed(restarted, press(f"ce:dn:ru:{CODE}:7:1:2"))
    assert len(core.bodies(TRANSLATION)) == 1 and edited(telegram)[0].startswith(SAVED)


@pytest.mark.parametrize("update", [
    lambda: say("/find haus", message_id=70, from_user=OTHER),
    lambda: press(f"ce:dn:ru:{CODE}:7:1:2", from_user=OTHER),
    lambda: press(f"ce:o:{CODE}", from_user=user(7007, language_code="ru")),
])
async def test_anyone_but_the_owner_gets_the_default_answer(telegram, core, application, update):
    await feed(application, update())
    assert owner_paths(core) == []
    assert sent(telegram) == [START]


async def test_a_malformed_button_is_only_acknowledged(telegram, core, application):
    for data in ("ce:x", f"ce:o:{CODE}x", f"ce:dn:de:{CODE}:7:1:2", f"ce:dn:ru:{CODE}:07:1:2",
                 f"ce:dn:ru:{CODE}:7:3:2", f"ce:dn:ru:{CODE}:7:100:100", f"ce:rm:ru:{CODE}:7:0:2",
                 f"ce:gv:{CODE}:7:pt:xx", f"ce:gi:{CODE}:7:ge", f"ce:gf:{CODE}:7:zz", f"ce:c:{CODE}:{OWNER_ID}"):
        await feed(application, press(data))
    assert owner_paths(core) == []
    assert telegram.calls_to("sendMessage") == [] and telegram.calls_to("editMessageText") == []
    assert len(telegram.calls_to("answerCallbackQuery")) == 11


# -- buttons and parse modes (§4.6, ВП-16, R9-05) --------------------------------------------

def test_callback_data_fits_64_bytes_with_99_translations_and_the_largest_revision():
    card = wire_card(revision=9_223_372_036_854_775_807, translations={"ru": tuple(f"t{n}" for n in range(99)),
                                                                       "uk": (), "en": ()})
    view = list_view(card, "ru", "ru")
    data = [button.data for row in view.buttons for button in row]
    # ✏️ and ✖ on each row, ⬆ and ⬇ between the ends, one of them at each end; then ➕ and ⬅.
    assert len(data) == 4 * 97 + 3 * 2 + 2
    longest = max(len(item.encode("utf-8")) for item in data)
    assert longest == 57 <= CALLBACK_DATA_BYTES
    assert f"ce:rm:ru:{CODE}:9223372036854775807:99:99" in data
    for item in data:
        assert parse(item) is not None


def test_more_than_99_translations_get_no_edit_buttons():
    card = wire_card(translations={"ru": tuple(f"t{n}" for n in range(100)), "uk": (), "en": ()})
    view = list_view(card, "ru", "ru")
    assert [button.data for row in view.buttons for button in row] == [f"ce:c:{CODE}"]
    assert "Переводов больше 99" in view.text and "100. t99" in view.text


def test_a_long_list_is_cut_to_one_message():
    card = wire_card(translations={"ru": tuple("слово" * 20 for _ in range(60)), "uk": (), "en": ()})
    assert len(list_view(card, "ru", "ru").text) == 4096


def test_the_card_code_is_the_uuid_in_22_canonical_characters():
    for card_id in (CARD, UUID(int=0), UUID(int=2 ** 128 - 1), uuid4()):
        assert len(card_code(card_id)) == 22 and card_of(card_code(card_id)) == card_id
    assert card_of(CODE[:-1] + ("B" if CODE[-1] == "A" else "A")) != CARD
    assert card_of("x" * 21) is None and card_of(CODE + "=") is None


async def test_only_the_echo_has_a_parse_mode(telegram, core, application):
    await feed(application, say("/find haus", message_id=80))
    await feed(application, press(f"ce:o:{CODE}"))
    await feed(application, press(f"ce:tl:ru:{CODE}"))
    await feed(application, press(f"ce:ed:ru:{CODE}:7:1:2"))
    await feed(application, say("домик", message_id=81))
    await feed(application, press(f"ce:gf:{CODE}:8:pl"))
    with_mode = [call for call in telegram.calls if "parse_mode" in call.params]
    assert [call.params["parse_mode"] for call in with_mode] == ["MarkdownV2", "MarkdownV2"]
    assert all(call.params["text"].startswith("Текущее значение\n\n`") for call in with_mode)


async def test_the_trail_of_the_editor_names_no_chat_person_text_or_button(telegram, core, application,
                                                                            trail_lines):
    await feed(application, say("/find Haus-query", message_id=90))
    await feed(application, press(f"ce:ed:ru:{CODE}:7:1:2"))
    await feed(application, say("домик-secret", message_id=91))
    await feed(application, say("unrelated-secret", message_id=92))
    updates = trail_lines.events("b.update")
    assert [(line["kind"], line["fn"], line["outcome"]) for line in updates] == [
        ("message", "card_editor", "handled"), ("callback", "card_editor", "handled"),
        ("message", "card_editor", "handled"), ("message", "default", "handled")]
    assert {line["route"] for line in trail_lines.events("b.core")} >= {SEARCH, READ, TRANSLATION}
    everything = trail_lines.everything
    for value in (str(OWNER_ID), "Anna", "Haus-query", "домик", "secret", "дом", "das Haus", CODE, str(CARD),
                  f"{CALLBACK_PREFIX}:"):
        assert value not in everything, value


# -- grammar (§3.4, B3) ----------------------------------------------------------------------

def _core_grammar_fields() -> dict:
    tree = ast.parse((CORE_SRC / "wortly" / "dictionary" / "common_fields.py").read_text(encoding="utf-8"))
    [value] = [node.value for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "GRAMMAR_FIELDS" for target in node.targets)]
    return ast.literal_eval(value)


def test_the_bot_grammar_fields_are_the_core_grammar_fields():
    assert GRAMMAR_FIELDS == _core_grammar_fields()


@pytest.mark.parametrize("card_type", ["noun", "verb", "adjective"])
async def test_the_grammar_buttons_are_the_grammar_fields_of_the_type(telegram, core, application, card_type):
    core.cards[str(CARD)] = owner_card_wire(str(CARD), card_type=card_type)
    await feed(application, press(f"ce:g:{CODE}"))
    [answer] = telegram.calls_to("editMessageText")
    fields = [parse(data).field_name for data in buttons(answer) if data.startswith("ce:gf:")]
    assert tuple(fields) == _core_grammar_fields()[card_type]
    assert all(data.split(":")[3] == "7" for data in buttons(answer) if data.startswith("ce:gf:"))
    assert buttons(answer)[-1] == f"ce:c:{CODE}"


PUBLISHED_OPTIONS = {
    "singular": ("enter", "na", "un"), "plural": ("enter", "na", "un"), "infinitive": ("enter", "na", "un"),
    "positive": ("enter", "na", "un"), "preterite": ("enter", "na"), "comparative": ("enter", "na"),
    "superlative": ("enter", "na"), "participle_ii": ("enter", "na"), "gender": ("m", "f", "n", "na", "un"),
    "article": ("ap", "na", "un"), "separability": ("sp", "is", "un"),
}


@pytest.mark.parametrize(("field_name", "options"), PUBLISHED_OPTIONS.items())
def test_the_value_buttons_of_a_published_card(field_name, options):
    assert field_options(wire_card(), field_name) == options


@pytest.mark.parametrize(("participle", "options"), [
    (GrammarForm("applicable", "gegangen"), ("h", "s")),
    (GrammarForm("not_applicable"), ("h", "s", "na", "un")),
])
def test_the_auxiliary_buttons_follow_the_participle(participle, options):
    card = wire_card(card_type="verb", grammar={"participle_ii": participle, "auxiliary": GrammarForm("unknown")})
    assert field_options(card, "auxiliary") == options


@pytest.mark.parametrize(("card_type", "data", "field_name", "value"), [
    ("noun", "ge:m", "gender", {"status": "applicable", "value": "masculine"}),
    ("noun", "ge:f", "gender", {"status": "applicable", "value": "feminine"}),
    ("noun", "ge:n", "gender", {"status": "applicable", "value": "neuter"}),
    ("noun", "ge:na", "gender", {"status": "not_applicable", "value": None}),
    ("noun", "ge:un", "gender", {"status": "unknown", "value": None}),
    ("noun", "ar:ap", "article", {"status": "applicable", "value": None}),
    ("noun", "ar:un", "article", {"status": "unknown", "value": None}),
    ("noun", "pl:na", "plural", {"status": "not_applicable", "value": None}),
    ("verb", "ax:s", "auxiliary", {"status": "applicable", "value": "sein"}),
    ("verb", "ax:h", "auxiliary", {"status": "applicable", "value": "haben"}),
    ("verb", "sp:sp", "separability", "separable"),
    ("verb", "sp:is", "separability", "inseparable"),
    ("verb", "sp:un", "separability", "unknown"),
    ("adjective", "po:un", "positive", {"status": "unknown", "value": None}),
])
async def test_a_value_button_saves_its_form(telegram, core, application, card_type, data, field_name, value):
    core.cards[str(CARD)] = owner_card_wire(str(CARD), card_type=card_type)
    update = press(f"ce:gv:{CODE}:7:{data}", callback_id="gv-1")
    await feed(application, update)
    [body] = core.bodies(GRAMMAR)
    assert body == {"actor_telegram_id": str(OWNER_ID), "command_id": str(command_id(Update.de_json(update,
                                                                                                    application.bot))),
                    "card_id": str(CARD), "field_name": field_name, "value": value, "expected_card_revision": "7"}
    assert owner_paths(core) == [GRAMMAR]
    [answer] = telegram.calls_to("editMessageText")
    assert answer.params["text"].startswith(f"{SAVED}\n\n")
    assert all(item.split(":")[3] == "8" for item in buttons(answer) if item.startswith("ce:gf:"))


async def test_a_text_field_shows_its_echo_and_the_typed_form_is_saved(telegram, core, application):
    await feed(application, press(f"ce:gf:{CODE}:7:pl"))
    echo, options = telegram.calls_to("sendMessage")
    assert echo.params["text"] == "Текущее значение\n\n`Häuser`\n\nНажми на значение, чтобы скопировать\\."
    assert options.params["text"] == "das Haus — мн. ч.: Häuser. Выбери:"
    assert buttons(options) == [f"ce:gi:{CODE}:7:pl", f"ce:gv:{CODE}:7:pl:na", f"ce:gv:{CODE}:7:pl:un", f"ce:g:{CODE}"]
    await feed(application, press(f"ce:gi:{CODE}:7:pl"))
    assert sent(telegram)[-1] == "Пришли форму «мн. ч.» одной строкой."
    await feed(application, say("Hauser\nHäuser", message_id=95))
    assert sent(telegram)[-1] == "Пришли форму одной строкой."
    await feed(application, say("Häuser ", message_id=96))
    [body] = core.bodies(GRAMMAR)
    assert (body["field_name"], body["value"]) == ("plural", {"status": "applicable", "value": "Häuser "})
    assert sent(telegram)[-1].startswith(f"{SAVED}\n\ndas Haus — грамматика:")


async def test_a_closed_field_has_no_echo(telegram, core, application):
    await feed(application, press(f"ce:gf:{CODE}:7:ge"))
    [options] = telegram.calls_to("sendMessage")
    assert buttons(options) == [f"ce:gv:{CODE}:7:ge:{code}" for code in ("m", "f", "n", "na", "un")] + [f"ce:g:{CODE}"]


async def test_a_refused_grammar_value_and_a_stale_grammar_button(telegram, core, application):
    core.cards[str(CARD)] = owner_card_wire(str(CARD), card_type="verb")
    await feed(application, press(f"ce:gv:{CODE}:7:pt:na"))
    core.answer(GRAMMAR, envelope(422, "VALIDATION_FAILED"))
    await feed(application, press(f"ce:gv:{CODE}:8:ax:un"))
    assert sent(telegram)[-1].startswith("Ядро не приняло значение")
    await feed(application, press(f"ce:gv:{CODE}:7:if:na"))
    assert edited(telegram)[-1].startswith(f"{CONFLICT}\n\ndas Haus — грамматика:")
    edits, messages = len(edited(telegram)), len(sent(telegram))
    await feed(application, press(f"ce:gf:{CODE}:7:if"))
    # The stale field button redraws the grammar in its message: no echo, no value buttons.
    assert len(edited(telegram)) == edits + 1 and len(sent(telegram)) == messages
    assert edited(telegram)[-1].startswith(f"{CONFLICT}\n\ndas Haus — грамматика:")
    assert owner_paths(core)[-1] == READ


@pytest.mark.parametrize(("card_type", "field_code", "view"), [
    ("verb", "ge", "das Haus — грамматика:"), ("noun", "ax", "das Haus — грамматика:"),
    ("phrase", "pl", "das Haus — фраза, A1"),
])
async def test_a_field_button_of_another_type_is_answered_as_stale(telegram, core, application, trail_lines,
                                                                   card_type, field_code, view):
    core.cards[str(CARD)] = owner_card_wire(str(CARD), card_type=card_type)
    await feed(application, press(f"ce:gf:{CODE}:7:{field_code}"))
    assert owner_paths(core) == [READ]
    assert sent(telegram) == []
    [answer] = edited(telegram)
    assert answer.startswith(f"Эта кнопка устарела. Показываю актуальные данные — повтори действие.\n\n{view}")
    assert [line["outcome"] for line in trail_lines.events("b.update")] == ["handled"]


def test_a_view_is_plain_text_with_callback_buttons_only():
    for view in (list_view(wire_card(), "ru", "ru"), grammar_view(wire_card(), "ru")):
        assert isinstance(view, View) and view.text.strip()
        assert all(button.data.startswith("ce:") for row in view.buttons for button in row)


def test_the_shipped_service_package_declares_the_editor():
    [function] = [item for item in discover() if item.name == "card_editor"]
    assert (function.commands, function.callback_prefix, function.audience) == (("find",), "ce", "owner")
    assert function.text_input is not None
    assert card_editor.SERVICE_FUNCTIONS == (function,)
    assert sorted(item.name for item in discover()) == ["access", "card_editor"]


async def test_the_same_grammar_text_twice_is_the_same_command(telegram, core, application):
    await feed(application, press(f"ce:gi:{CODE}:7:sg"))
    message = say("Häuschen", message_id=97)
    await feed(application, message)
    core.cards[str(CARD)] = owner_card_wire(str(CARD), revision=15)
    await feed(application, message)
    first, second = core.bodies(GRAMMAR)
    assert first == second
    assert first["command_id"] == str(command_id(Update.de_json(message, application.bot)))
    assert sent(telegram)[-2].startswith(SAVED) and "Сохранено" not in sent(telegram)[-1]
    await feed(application, say("Häuslein", message_id=98))
    assert sent(telegram)[-1] == START and len(core.bodies(GRAMMAR)) == 2


def test_a_notice_over_a_full_view_still_fits_one_message():
    full = View("x" * TEXT_MAX_CHARACTERS)
    noticed = with_notice("Карточку уже изменили.", full)
    assert len(noticed.text) == TEXT_MAX_CHARACTERS and noticed.text.endswith("…")
    assert noticed.text.startswith("Карточку уже изменили.\n\nx")
