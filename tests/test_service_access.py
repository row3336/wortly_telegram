"""The owner's admission in the bot (answer 2.6; design T-D §5.2, §5.3): decisions, the reason, the list.

Updates go through the real Application and router over the fake Bot API; the core is
the fake gateway, which records the principal of every call. The last part checks the
gateway's pending_requests over the core's internal API (FakeCore).
"""

from __future__ import annotations

import logging
from uuid import uuid4

import httpx
import pytest
from telegram import Update

from conftest import feed
from fakes.core_gateway import FakeCore, core_settings
from fakes.telegram_api import callback_update, message_update, user
from wortly.identity.principal import TelegramPrincipal
from wortly_telegram.gateway import CoreError, CoreUnavailable, PendingRequest, build_gateway
from wortly_telegram.router import command_id, discover
from wortly_telegram.service import access
from wortly_telegram.service.access import ReasonInputs, service_functions
from wortly_telegram.start import TEXTS as START_TEXTS


OWNER_ID, OTHER_ID = 5005, 6006
OWNER = user(OWNER_ID, language_code="ru")
REQUEST_ID = uuid4()
ADMITTED_OWNER = {"role": "owner", "status": "admitted", "ui_language": "ru"}
ORIGINAL = "old"  # the text of the pressed message in fakes.telegram_api.callback_update


def applied(status: str = "applied") -> dict:
    return {"command": {"id": str(uuid4()), "status": status, "applied_at": "2026-10-06T12:00:00Z",
                        "target_id": str(REQUEST_ID), "object_version": "2"},
            "current": {"profile_id": str(uuid4()), "access": {"status": "admitted", "version": "4", "reason": None}}}


@pytest.fixture
def owner_gateway(gateway):
    gateway.summaries[str(OWNER_ID)] = ADMITTED_OWNER
    gateway.result = applied()
    return gateway


@pytest.fixture
def inputs() -> ReasonInputs:
    return ReasonInputs()


@pytest.fixture
async def application(make_application, owner_gateway, inputs):
    return await make_application(functions=service_functions(inputs))


def press(action: str, *, version: int = 1, from_user: dict = OWNER, callback_id: str = "901", **extra) -> dict:
    return callback_update(f"acc:{action}:{REQUEST_ID}:{version}", from_user=from_user, callback_id=callback_id,
                           **extra)


def decisions(gateway) -> list:
    return [call for call in gateway.calls if call.operation == "decide"]


def texts(telegram, method: str = "sendMessage") -> list[str]:
    return [call.params.get("text") for call in telegram.calls_to(method)]


def callback_texts(telegram) -> list[str | None]:
    return [call.params.get("text") for call in telegram.calls_to("answerCallbackQuery")]


# -- "Approve" and "Reject" ---------------------------------------------------------------

@pytest.mark.parametrize(("action", "approve", "outcome"), [("a", True, "✅ Доступ выдан"), ("r", False, "❌ Отклонён")])
async def test_a_decision_goes_to_the_core_and_the_pressed_message_gets_the_outcome(
        telegram, owner_gateway, application, action, approve, outcome):
    data = press(action, version=3)
    await feed(application, data)
    [call] = decisions(owner_gateway)
    assert call.principal == TelegramPrincipal(str(OWNER_ID))
    assert call.arguments == {"command_id": command_id(Update.de_json(data, application.bot)),
                              "request_id": REQUEST_ID, "expected_version": 3, "approve": approve, "reason": None}
    assert callback_texts(telegram) == [None]
    [edit] = telegram.calls_to("editMessageText")
    assert edit.params["text"] == f"{ORIGINAL}\n\n{outcome}"
    # Without reply_markup Telegram drops the inline keyboard: the buttons are gone.
    assert "reply_markup" not in edit.params
    # Q31 is open: the applicant gets nothing from the bot (the login button is the core's access_approved job).
    assert telegram.calls_to("sendMessage") == []


async def test_the_same_press_delivered_twice_sends_nothing_the_second_time(telegram, owner_gateway, application):
    data = press("a")
    await feed(application, data)
    owner_gateway.result = applied("already_applied")
    await feed(application, data)
    first, second = decisions(owner_gateway)
    assert first.arguments["command_id"] == second.arguments["command_id"]
    assert len(telegram.calls_to("editMessageText")) == 1
    assert callback_texts(telegram) == [None, None]
    assert telegram.calls_to("sendMessage") == []


@pytest.mark.parametrize(("error", "notice", "edited"), [
    (CoreError("VERSION_CONFLICT", status=409), None, f"{ORIGINAL}\n\nℹ️ Уже обработано"),
    (CoreError("FORBIDDEN", status=403), "⛔ Нет прав.", None),
    (CoreError("WRITE_UNAVAILABLE", status=503), "Сервис недоступен, повторите позже.", None),
    (CoreUnavailable(), "Сервис недоступен, повторите позже.", None),
    (CoreError("COMMAND_PAYLOAD_MISMATCH", status=409), "Сервис недоступен, повторите позже.", None),
])
async def test_the_core_refusals_become_answers(telegram, owner_gateway, application, error, notice, edited):
    owner_gateway.errors["decide"] = error
    await feed(application, press("a"))
    assert callback_texts(telegram) == [notice]
    assert texts(telegram, "editMessageText") == ([edited] if edited else [])
    assert telegram.calls_to("sendMessage") == []


async def test_an_answer_outside_the_contract_is_try_later(telegram, owner_gateway, application):
    owner_gateway.result = {"command": {"status": "queued"}, "current": {}}
    await feed(application, press("a"))
    assert callback_texts(telegram) == ["Сервис недоступен, повторите позже."]
    assert telegram.calls_to("editMessageText") == []


# -- a request gone with its applicant's deleted profile (P04 §3.3, PD-19) -------------------

GONE = {"ru": "ℹ️ Заявка больше недоступна", "uk": "ℹ️ Заявка більше недоступна",
        "en": "ℹ️ The request is no longer available"}


def gone() -> dict:
    """The core's answer to the repeat of a decision whose request went with the applicant's profile."""
    return {"command": {"id": str(uuid4()), "status": "already_applied", "applied_at": "2026-10-06T12:00:00Z",
                        "target_id": str(REQUEST_ID), "object_version": "2"},
            "current": {"status": "unavailable"}}


@pytest.mark.parametrize("language", ["ru", "uk", "en"])
@pytest.mark.parametrize("action", ["a", "r"])
async def test_the_repeat_on_a_deleted_applicants_request_says_it_is_gone_and_drops_the_buttons(
        telegram, owner_gateway, application, language, action):
    owner_gateway.summaries[str(OWNER_ID)] = {**ADMITTED_OWNER, "ui_language": language}
    owner_gateway.result = gone()
    await feed(application, press(action))
    [call] = decisions(owner_gateway)
    assert call.principal == TelegramPrincipal(str(OWNER_ID))
    assert callback_texts(telegram) == [GONE[language]]
    # The buttons go; the text of the message is not touched.
    [markup] = telegram.calls_to("editMessageReplyMarkup")
    assert markup.params["message_id"] == "1" and "reply_markup" not in markup.params
    assert telegram.calls_to("editMessageText") == []
    assert telegram.calls_to("sendMessage") == []


@pytest.mark.parametrize("current", [None, {}, {"status": "other"}, {"status": "unavailable", "x": 1}, "unavailable"],
                         ids=["null", "empty", "other-status", "extra-field", "string"])
async def test_only_current_unavailable_is_the_gone_answer(telegram, owner_gateway, application, current):
    # The repeat of an ordinary decision stays silent; an unknown "current" never invents "gone".
    owner_gateway.result = {**applied("already_applied"), "current": current}
    await feed(application, press("a"))
    assert callback_texts(telegram) == [None]
    assert telegram.calls_to("editMessageText") == []
    assert telegram.calls_to("sendMessage") == []


async def test_a_repeat_after_the_outcome_was_shown_keeps_the_outcome_text(telegram, owner_gateway, application):
    # The same update comes again after the applicant deleted the profile: its message text is the one from
    # before the outcome, so rewriting it would erase "access granted". Only the buttons are taken off.
    data = press("a")
    await feed(application, data)
    [first] = telegram.calls_to("editMessageText")
    assert first.params["text"] == f"{ORIGINAL}\n\n✅ Доступ выдан"
    owner_gateway.result = gone()
    await feed(application, data)
    first_call, second_call = decisions(owner_gateway)
    assert first_call.arguments["command_id"] == second_call.arguments["command_id"]
    assert len(telegram.calls_to("editMessageText")) == 1  # nothing rewrote the outcome
    assert len(telegram.calls_to("editMessageReplyMarkup")) == 1
    assert callback_texts(telegram) == [None, GONE["ru"]]


@pytest.mark.parametrize(("action", "outcome"), [("a", "✅ Доступ выдан"), ("r", "❌ Отклонён")])
async def test_an_applied_status_is_applied_whatever_current_says(telegram, owner_gateway, application, action, outcome):
    # The core cannot answer applied with current unavailable; if it did, the status decides, not "current".
    answer = gone()
    answer["command"]["status"] = "applied"
    owner_gateway.result = answer
    await feed(application, press(action))
    assert callback_texts(telegram) == [None]
    [edit] = telegram.calls_to("editMessageText")
    assert edit.params["text"] == f"{ORIGINAL}\n\n{outcome}"
    assert telegram.calls_to("editMessageReplyMarkup") == []


async def test_a_reason_applied_with_current_unavailable_is_still_applied(telegram, owner_gateway, application):
    await feed(application, press("w"))
    answer = gone()
    answer["command"]["status"] = "applied"
    owner_gateway.result = answer
    await feed(application, say("причина"))
    assert texts(telegram)[-1] == "❌ Отклонён\nПричина: причина"
    assert len(telegram.calls_to("editMessageText")) == 1 and telegram.calls_to("editMessageReplyMarkup") == []


async def test_the_repeat_with_a_reason_on_a_gone_request_says_so_and_ends_the_input(
        telegram, owner_gateway, application):
    await feed(application, press("w"))
    owner_gateway.result = gone()
    await feed(application, say("причина"))
    assert texts(telegram)[-1] == GONE["ru"]
    assert len(telegram.calls_to("editMessageReplyMarkup")) == 1
    assert telegram.calls_to("editMessageText") == []
    # The input is over: the next plain text is not a reason.
    owner_gateway.errors.clear()
    await feed(application, say("ещё", message_id=51))
    assert len(decisions(owner_gateway)) == 1


@pytest.mark.parametrize("action", ["a", "r"])
async def test_a_new_press_on_a_request_gone_is_forbidden_without_a_write(telegram, owner_gateway, application, action):
    owner_gateway.errors["decide"] = CoreError("FORBIDDEN", status=403)
    await feed(application, press(action, callback_id="905"))
    assert callback_texts(telegram) == ["⛔ Нет прав."]
    # One decision asked (the router's own summary and contact note aside), nothing edited or sent: only the notice.
    assert [call.operation for call in owner_gateway.calls
            if call.operation not in ("profile_summary", "note_contact")] == ["decide"]
    assert telegram.calls_to("editMessageText") == [] and telegram.calls_to("sendMessage") == []


async def test_a_reason_on_a_request_gone_is_forbidden_and_ends_the_input(telegram, owner_gateway, application):
    await feed(application, press("w"))
    owner_gateway.errors["decide"] = CoreError("FORBIDDEN", status=403)
    await feed(application, say("причина"))
    assert texts(telegram)[-1] == "⛔ Нет прав."
    assert telegram.calls_to("editMessageText") == []
    owner_gateway.errors.clear()
    await feed(application, say("ещё", message_id=51))
    assert len(decisions(owner_gateway)) == 1


async def test_the_gone_answer_of_the_core_reaches_the_bot_over_the_internal_api(core, http_gateway):
    core.answer("/internal/v1/admission/decide", httpx.Response(200, json=gone()))
    answer = await http_gateway.decide(TelegramPrincipal(str(OWNER_ID)), command_id=uuid4(),
                                            request_id=REQUEST_ID, expected_version=1, approve=True, reason=None)
    assert answer["current"] == {"status": "unavailable"}
    assert answer["command"]["status"] == "already_applied"


# -- the actor is the sender, never the button ------------------------------------------

async def test_the_actor_is_the_sender_of_the_press_not_the_chat_or_the_data(owner_gateway, application):
    await feed(application, press("a", chat_id=OTHER_ID))
    [call] = decisions(owner_gateway)
    assert call.principal == TelegramPrincipal(str(OWNER_ID))


async def test_a_button_carrying_anything_more_than_references_decides_nothing(
        telegram, owner_gateway, application):
    for data in (f"acc:a:{REQUEST_ID}:1:{OTHER_ID}", f"acc:a:{OTHER_ID}:1", f"acc:a:{REQUEST_ID}:0",
                 f"acc:a:{REQUEST_ID}:01", f"acc:x:{REQUEST_ID}:1", "acc:l:1", f"access:approve:{OTHER_ID}"):
        await feed(application, callback_update(data, from_user=OWNER))
    assert decisions(owner_gateway) == []
    assert all(text is None for text in callback_texts(telegram))


@pytest.mark.parametrize(("summary", "language"), [
    ({"role": "user", "status": "admitted", "ui_language": "uk"}, "uk"),
    ({"role": "owner", "status": "pending", "ui_language": "uk"}, "uk"),
    (None, "en"),
])
async def test_a_press_of_anyone_but_an_admitted_owner_reaches_no_decision(
        telegram, owner_gateway, application, summary, language):
    owner_gateway.summaries[str(OTHER_ID)] = summary
    await feed(application, press("a", from_user=user(OTHER_ID)))
    assert decisions(owner_gateway) == []
    # The router's default answer (ВП-27), as for any button the sender may not use.
    assert texts(telegram) == [START_TEXTS[language]]


# -- "Reject with a reason" ------------------------------------------------------------

PROMPT = "✍️ Напишите причину отказа одним сообщением (до 500 символов). Команда или кнопка отменяет ввод."


def say(text: str, *, from_user: dict = OWNER, message_id: int = 50) -> dict:
    return message_update(text, from_user=from_user, message_id=message_id)


async def test_the_next_text_of_the_owner_is_the_reason(telegram, owner_gateway, application):
    await feed(application, press("w", version=4))
    assert decisions(owner_gateway) == []
    assert texts(telegram) == [PROMPT]
    owner_gateway.result = applied()
    reason = say("Не знаю вас")
    await feed(application, reason)
    [call] = decisions(owner_gateway)
    assert call.principal == TelegramPrincipal(str(OWNER_ID))
    assert call.arguments == {"command_id": command_id(Update.de_json(reason, application.bot)),
                              "request_id": REQUEST_ID, "expected_version": 4, "approve": False,
                              "reason": "Не знаю вас"}
    outcome = "❌ Отклонён\nПричина: Не знаю вас"
    [edit] = telegram.calls_to("editMessageText")
    assert (edit.params["message_id"], edit.params["text"]) == ("1", f"{ORIGINAL}\n\n{outcome}")
    assert texts(telegram) == [PROMPT, outcome]
    # The input is over: the next text is just a text.
    await feed(application, say("ещё", message_id=51))
    assert len(decisions(owner_gateway)) == 1
    assert texts(telegram)[-1] == START_TEXTS["ru"]


async def test_a_reason_of_500_characters_is_taken_and_501_is_not(telegram, owner_gateway, application):
    await feed(application, press("w"))
    await feed(application, say("я" * 501))
    assert decisions(owner_gateway) == []
    assert texts(telegram)[-1] == "Причина длиннее 500 символов (501). Напишите короче."
    # Still waiting; 500 Cyrillic letters are 1000 bytes and still 500 characters.
    await feed(application, say("я" * 500, message_id=51))
    [call] = decisions(owner_gateway)
    assert call.arguments["reason"] == "я" * 500


async def test_a_reason_the_core_refuses_asks_for_another_one(telegram, owner_gateway, application):
    await feed(application, press("w"))
    owner_gateway.errors["decide"] = CoreError("VALIDATION_FAILED", status=422)
    await feed(application, say("короткая"))
    # Within the local bound the refusal is the core's other rule, not the length (T2-QA З5).
    assert texts(telegram)[-1] == "Причина не принята ядром, попробуйте другую формулировку."
    owner_gateway.errors.clear()
    await feed(application, say("вторая", message_id=51))
    assert decisions(owner_gateway)[-1].arguments["reason"] == "вторая"


async def test_an_unavailable_core_keeps_the_input(telegram, owner_gateway, application):
    await feed(application, press("w"))
    owner_gateway.errors["decide"] = CoreUnavailable()
    await feed(application, say("причина"))
    assert texts(telegram)[-1] == "Сервис недоступен, повторите позже."
    owner_gateway.errors.clear()
    await feed(application, say("причина", message_id=51))
    assert texts(telegram)[-1] == "❌ Отклонён\nПричина: причина"


@pytest.mark.parametrize(("error", "answer", "edited"), [
    (CoreError("VERSION_CONFLICT", status=409), "ℹ️ Уже обработано", True),
    (CoreError("FORBIDDEN", status=403), "⛔ Нет прав.", False),
])
async def test_a_refused_reason_ends_the_input(telegram, owner_gateway, application, error, answer, edited):
    await feed(application, press("w"))
    owner_gateway.errors["decide"] = error
    await feed(application, say("причина"))
    assert texts(telegram)[-1] == answer
    assert len(telegram.calls_to("editMessageText")) == (1 if edited else 0)
    owner_gateway.errors.clear()
    await feed(application, say("ещё", message_id=51))
    assert len(decisions(owner_gateway)) == 1


async def test_the_same_reason_delivered_twice_sends_nothing_the_second_time(telegram, owner_gateway, inputs,
                                                                              application):
    await feed(application, press("w"))
    reason = say("причина")
    await feed(application, reason)
    sent = len(telegram.calls)
    # A second poller delivers the same message: the input was already over, so it is a plain text.
    owner_gateway.result = applied("already_applied")
    await feed(application, press("w", callback_id="902"))
    await feed(application, reason)
    assert decisions(owner_gateway)[-1].arguments["command_id"] == decisions(owner_gateway)[0].arguments["command_id"]
    assert texts(telegram)[-1] == PROMPT
    assert len(telegram.calls_to("editMessageText")) == 1
    assert len(telegram.calls) == sent + 2  # the second prompt's answer and message only


@pytest.mark.parametrize("interruption", [
    lambda: message_update("/start", from_user=OWNER, message_id=60),
    lambda: message_update("/undo", from_user=OWNER, message_id=60),
    lambda: callback_update("acc:l", from_user=OWNER, callback_id="903"),
    lambda: callback_update("probe:old", from_user=OWNER, callback_id="904"),
])
async def test_a_command_or_a_button_cancels_the_input(telegram, owner_gateway, application, interruption):
    await feed(application, press("w"))
    await feed(application, interruption())
    await feed(application, say("причина"))
    assert decisions(owner_gateway) == []
    assert texts(telegram)[-1] == START_TEXTS["ru"]


async def test_another_reason_button_replaces_the_input(owner_gateway, application):
    other = uuid4()
    await feed(application, press("w"))
    await feed(application, callback_update(f"acc:w:{other}:2", from_user=OWNER, callback_id="905"))
    await feed(application, say("причина"))
    [call] = decisions(owner_gateway)
    assert (call.arguments["request_id"], call.arguments["expected_version"]) == (other, 2)


async def test_a_text_of_someone_else_is_never_the_reason(telegram, owner_gateway, application):
    owner_gateway.summaries[str(OTHER_ID)] = {"role": "owner", "status": "admitted", "ui_language": "ru"}
    await feed(application, press("w"))
    await feed(application, say("чужая причина", from_user=user(OTHER_ID)))
    assert decisions(owner_gateway) == []
    assert texts(telegram)[-1] == START_TEXTS["ru"]
    # The owner's own input is still there.
    await feed(application, say("моя причина", message_id=51))
    [call] = decisions(owner_gateway)
    assert call.principal == TelegramPrincipal(str(OWNER_ID)) and call.arguments["reason"] == "моя причина"


async def test_a_text_of_the_owner_in_a_group_is_never_the_reason(telegram, owner_gateway, inputs, application):
    await feed(application, press("w"))
    await feed(application, message_update("групповая", from_user=OWNER, chat_id=-1001, chat_type="group"))
    assert decisions(owner_gateway) == []
    # The private input is still there and takes the next private text.
    await feed(application, say("личная", message_id=51))
    [call] = decisions(owner_gateway)
    assert call.arguments["reason"] == "личная"


async def test_a_refused_edit_still_answers_the_owner_and_logs_the_refusal(telegram, owner_gateway, inputs,
                                                                         application, caplog):
    await feed(application, press("w"))
    telegram.fail("editMessageText", 400, "Bad Request: message to edit not found")
    with caplog.at_level(logging.DEBUG, logger="wortly_telegram"):
        await feed(application, say("причина"))
    assert texts(telegram)[-1] == "❌ Отклонён\nПричина: причина"
    assert not inputs.waiting(Update.de_json(say("ещё", message_id=51), application.bot))
    lines = [record.getMessage() for record in caplog.records if record.name.startswith("wortly_telegram")]
    assert lines == ["reply refused by Telegram; not repeated: BadRequest"]


async def test_an_owner_who_lost_the_right_meanwhile_gives_no_reason(telegram, owner_gateway, application):
    await feed(application, press("w"))
    owner_gateway.summaries[str(OWNER_ID)] = {"role": "user", "status": "admitted", "ui_language": "ru"}
    await feed(application, say("причина"))
    owner_gateway.summaries[str(OWNER_ID)] = ADMITTED_OWNER
    await feed(application, say("ещё", message_id=51))
    assert decisions(owner_gateway) == []
    assert texts(telegram)[-2:] == [START_TEXTS["ru"], START_TEXTS["ru"]]


async def test_a_restart_loses_the_input(telegram, owner_gateway, make_application, application):
    await feed(application, press("w"))
    restarted = await make_application(functions=service_functions(ReasonInputs()))
    await feed(restarted, say("причина"))
    assert decisions(owner_gateway) == []
    assert texts(telegram)[-1] == START_TEXTS["ru"]


# -- "Show requests" -------------------------------------------------------------------

def pending(number: int, *, first_name: str | None, username: str | None) -> PendingRequest:
    return PendingRequest(uuid4(), number, str(7000 + number), first_name, username, "2026-10-06T10:00:00Z")


async def test_the_list_is_one_message_per_pending_request_with_its_buttons(telegram, owner_gateway, application):
    owner_gateway.pending = [pending(1, first_name="Anna", username="anna_k"),
                             pending(2, first_name="<b>Bob</b>", username=None)]
    await feed(application, callback_update("acc:l", from_user=OWNER))
    [call] = [call for call in owner_gateway.calls if call.operation == "pending_requests"]
    assert call.principal == TelegramPrincipal(str(OWNER_ID))
    assert callback_texts(telegram) == [None]
    assert texts(telegram) == ["⏰ 2 заявок на доступ ждут решения:", "• @anna_k | Anna | 7001",
                               "• <b>Bob</b> | 7002"]
    for message, request in zip(telegram.calls_to("sendMessage")[1:], owner_gateway.pending):
        assert message.json("reply_markup")["inline_keyboard"] == [
            [{"text": "✅ Разрешить", "callback_data": f"acc:a:{request.request_id}:{request.version}"},
             {"text": "❌ Отклонить", "callback_data": f"acc:r:{request.request_id}:{request.version}"}],
            [{"text": "✍️ Отклонить с причиной", "callback_data": f"acc:w:{request.request_id}:{request.version}"}],
        ]


async def test_an_empty_list_says_so(telegram, owner_gateway, application):
    await feed(application, callback_update("acc:l", from_user=OWNER))
    assert texts(telegram) == ["Нет заявок в ожидании."]


@pytest.mark.parametrize(("error", "notice"), [
    (CoreError("FORBIDDEN", status=403), "⛔ Нет прав."),
    (CoreUnavailable(), "Сервис недоступен, повторите позже."),
])
async def test_a_refused_list_is_a_notice(telegram, owner_gateway, application, error, notice):
    owner_gateway.errors["pending_requests"] = error
    await feed(application, callback_update("acc:l", from_user=OWNER))
    assert callback_texts(telegram) == [notice]
    assert telegram.calls_to("sendMessage") == []


def test_the_shipped_service_package_declares_the_access_function():
    # The package also holds the owner's common editor (service/card_editor.py, DEV-17 B2).
    [function] = [item for item in discover() if item.name == "access"]
    assert (function.name, function.commands, function.callback_prefix, function.audience) == (
        "access", (), "acc", "owner")
    assert function.text_input is not None
    assert access.SERVICE_FUNCTIONS == (function,)


# -- the gateway's pending_requests over the internal API (C-ADAPTER-3) -----------------------

WIRE = {"request_id": str(REQUEST_ID), "request_version": "3",
        "applicant": {"telegram_user_id": "7001", "first_name": "Anna", "username": None},
        "requested_at": "2026-10-06T10:00:00Z"}


@pytest.fixture
def core() -> FakeCore:
    return FakeCore()


@pytest.fixture
async def http_gateway(core):
    gateway = build_gateway(core_settings(), transport=core.transport())
    yield gateway
    await gateway.aclose()


async def test_pending_requests_posts_the_actor_and_reads_the_list(core, http_gateway):
    core.pending = [WIRE]
    [request] = await http_gateway.pending_requests(TelegramPrincipal(str(OWNER_ID)))
    assert core.bodies("/internal/v1/admission/pending_requests") == [{"actor_telegram_id": str(OWNER_ID)}]
    assert request == PendingRequest(REQUEST_ID, 3, "7001", "Anna", None, "2026-10-06T10:00:00Z")
    # Names and IDs of the applicant are personal data: never in a repr (TC:273).
    assert "Anna" not in repr(request) and "7001" not in repr(request)


async def test_pending_requests_refused_is_the_core_code(core, http_gateway):
    core.answer("/internal/v1/admission/pending_requests", httpx.Response(403, json={"error": {"code": "FORBIDDEN"}}))
    with pytest.raises(CoreError) as refused:
        await http_gateway.pending_requests(TelegramPrincipal(str(OWNER_ID)))
    assert (refused.value.code, refused.value.status) == ("FORBIDDEN", 403)
    assert not isinstance(refused.value, CoreUnavailable)


@pytest.mark.parametrize("answer", [
    {"requests": {}},
    {"requests": [], "extra": 1},
    {"requests": [{**WIRE, "extra": 1}]},
    {"requests": [{**WIRE, "request_version": 3}]},
    {"requests": [{**WIRE, "request_version": "0"}]},
    {"requests": [{**WIRE, "request_id": "not-a-uuid"}]},
    {"requests": [{**WIRE, "requested_at": None}]},
    {"requests": [{**WIRE, "applicant": {"telegram_user_id": 7001, "first_name": None, "username": None}}]},
    {"requests": [{**WIRE, "applicant": {"telegram_user_id": None, "first_name": None}}]},
])
async def test_a_list_outside_the_contract_is_core_unavailable(core, http_gateway, answer):
    core.answer("/internal/v1/admission/pending_requests", httpx.Response(200, json=answer))
    with pytest.raises(CoreUnavailable):
        await http_gateway.pending_requests(TelegramPrincipal(str(OWNER_ID)))
