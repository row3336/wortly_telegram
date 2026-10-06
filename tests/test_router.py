"""Router: service functions, audience as an interface filter, the default answer (ВП-27), command_id (ВП-6)."""

from __future__ import annotations

import asyncio
import logging
import sys
import textwrap

import pytest
from telegram import Update

from conftest import feed
from fakes.core_gateway import FakeCoreGateway, GatewayCall
from fakes.telegram_api import WEB_APP_URL, callback_update, message_update, user
from wortly_telegram.gateway import principal_of
from wortly_telegram.router import (
    RouterError, ServiceFunction, command_id, discover,
)
from wortly_telegram.start import TEXTS


class Probe:
    """A service function that records the subject and the command it was given."""

    def __init__(self, gateway) -> None:
        self.gateway = gateway
        self.seen: list[tuple[str, object]] = []

    async def handle(self, update, context) -> None:
        principal = principal_of(update)
        await context.gateway.profile_summary(principal)
        self.seen.append((principal.external_id, command_id(update)))


class ContactGateway(FakeCoreGateway):
    """The shared fake plus a shared event list (to prove the order) and a failure only for note_contact."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str]] = []
        self.contact_error: Exception | None = None

    async def note_contact(self, principal) -> None:
        self.events.append(("contact", principal.external_id))
        if self.contact_error is not None:
            self.calls.append(GatewayCall("note_contact", principal))
            raise self.contact_error
        await super().note_contact(principal)


@pytest.fixture
def gateway() -> ContactGateway:
    return ContactGateway()


@pytest.fixture
def events(telegram, gateway) -> list[tuple[str, str]]:
    """What happened in what order: a contact note, an answer sent to Telegram, a handler run."""
    telegram.on_request = lambda call: gateway.events.append(("telegram", call.method)) if call.method in (
        "sendMessage", "answerCallbackQuery") else None
    return gateway.events


def contacts(gateway) -> list[str]:
    return [call.principal.external_id for call in gateway.calls if call.operation == "note_contact"]


def probe_function(probe: Probe, audience: str = "anyone") -> ServiceFunction:
    return ServiceFunction("probe", ("probe",), "probe", audience, probe.handle)


# -- the default answer, ВП-27 ------------------------------------------------------------

@pytest.mark.parametrize("text", ["hello", "/undo", "/quiz", "Start"])
async def test_any_other_text_gets_the_short_answer_with_the_app_button(telegram, make_application, text):
    application = await make_application()
    await feed(application, message_update(text, from_user=user(5005, language_code="en")))
    [call] = telegram.calls_to("sendMessage")
    assert call.params["text"] == TEXTS["en"]
    [[button]] = call.json("reply_markup")["inline_keyboard"]
    assert button["web_app"] == {"url": WEB_APP_URL}


async def test_an_old_button_is_acknowledged_and_gets_the_short_answer(telegram, make_application):
    application = await make_application()
    await feed(application, callback_update("access:approve:12345", from_user=user(5005)))
    assert len(telegram.calls_to("answerCallbackQuery")) == 1
    assert len(telegram.calls_to("sendMessage")) == 1


async def test_non_text_messages_and_groups_get_no_answer(telegram, make_application):
    application = await make_application()
    sticker = message_update("x", from_user=user(5005))
    del sticker["message"]["text"]
    sticker["message"]["sticker"] = {"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1,
                                     "is_animated": False, "is_video": False, "type": "regular"}
    await feed(application, sticker)
    await feed(application, message_update("hello", from_user=user(5005), chat_id=-1001, chat_type="group"))
    assert telegram.calls == [call for call in telegram.calls if call.method == "getMe"]


# -- contact: any private message or button press lifts the "bot blocked" mark (answer 2.5) ----

UPDATES = [
    ("text", lambda: message_update("hello", from_user=user(5005))),
    ("start", lambda: message_update("/start", from_user=user(5005))),
    ("old-command", lambda: message_update("/undo", from_user=user(5005))),
    ("service-command", lambda: message_update("/probe", from_user=user(5005))),
    ("service-button", lambda: callback_update("probe:act:1", from_user=user(5005))),
    ("old-button", lambda: callback_update("access:approve:12345", from_user=user(5005))),
]


@pytest.mark.parametrize("make", [row[1] for row in UPDATES], ids=[row[0] for row in UPDATES])
async def test_every_private_message_and_button_press_is_one_contact_before_any_handler(
        gateway, events, make_application, make):
    probe = Probe(gateway)
    original = probe.handle

    async def traced(update, context):
        events.append(("handler", "probe"))
        await original(update, context)

    application = await make_application(functions=(ServiceFunction("probe", ("probe",), "probe", "anyone", traced),))
    await feed(application, make())
    assert contacts(gateway) == ["5005"]
    # The note comes first: before the service function and before any answer to the user.
    assert events[0] == ("contact", "5005")


async def test_a_private_message_without_text_is_a_contact_too(telegram, gateway, make_application):
    application = await make_application()
    sticker = message_update("x", from_user=user(5005))
    del sticker["message"]["text"]
    sticker["message"]["sticker"] = {"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1,
                                     "is_animated": False, "is_video": False, "type": "regular"}
    await feed(application, sticker)
    assert contacts(gateway) == ["5005"]
    assert telegram.calls_to("sendMessage") == []


async def test_the_sender_is_the_principal_of_the_contact(gateway, make_application):
    application = await make_application()
    await feed(application, callback_update("probe:act:987654321", from_user=user(5005)))
    await feed(application, message_update("hello 987654321", from_user=user(6006)))
    assert contacts(gateway) == ["5005", "6006"]


async def test_group_chats_and_edited_messages_are_no_contact(gateway, make_application):
    application = await make_application(functions=())
    await feed(application, message_update("hello", from_user=user(5005), chat_id=-1001, chat_type="group"))
    await feed(application, message_update("hello", from_user=user(5005), edited=True))
    assert contacts(gateway) == []


async def test_a_message_lifts_the_block_mark(gateway, make_application):
    gateway.blocked = {"5005", "6006"}
    application = await make_application()
    await feed(application, message_update("hello", from_user=user(5005)))
    assert gateway.blocked == {"6006"}


async def test_a_failing_contact_note_never_keeps_the_user_from_the_answer(telegram, gateway, make_application,
                                                                          caplog):
    gateway.contact_error = RuntimeError("host=db.internal password=hunter2 user text")
    probe = Probe(gateway)
    application = await make_application(functions=(probe_function(probe),))
    with caplog.at_level(logging.DEBUG):
        await feed(application, message_update("hello", from_user=user(5005)))
        await feed(application, message_update("/probe", from_user=user(5005)))
    assert len(telegram.calls_to("sendMessage")) == 1  # the default answer to "hello"
    assert len(probe.seen) == 1  # the service function ran as well
    assert contacts(gateway) == ["5005", "5005"]
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "note_contact failed: RuntimeError" in logged
    assert "hunter2" not in logged and "db.internal" not in logged


async def test_a_hanging_contact_note_does_not_hold_the_answer_longer_than_the_timeout(
        telegram, gateway, make_application, monkeypatch, caplog):
    from wortly_telegram import router

    async def hang(principal) -> None:
        await asyncio.sleep(30)

    gateway.note_contact = hang
    monkeypatch.setattr(router, "CONTACT_TIMEOUT", 0.05)
    application = await make_application()
    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(feed(application, message_update("hello", from_user=user(5005))), timeout=5)
    assert len(telegram.calls_to("sendMessage")) == 1
    assert "note_contact failed: TimeoutError" in " ".join(r.getMessage() for r in caplog.records)


# -- subject and command_id ---------------------------------------------------------------

async def test_the_subject_comes_from_the_sender_never_from_callback_data(gateway, make_application):
    probe = Probe(gateway)
    application = await make_application(functions=(probe_function(probe),))
    await feed(application, callback_update("probe:act:987654321", from_user=user(5005), callback_id="c-1"))
    await feed(application, message_update("/probe 987654321", from_user=user(6006)))
    assert [subject for subject, _ in probe.seen] == ["5005", "6006"]
    summaries = [call for call in gateway.calls if call.operation == "profile_summary"]
    assert [call.principal.external_id for call in summaries] == ["5005", "6006"]


async def test_the_same_update_twice_is_the_same_command(gateway, make_application):
    probe = Probe(gateway)
    application = await make_application(functions=(probe_function(probe),))
    press = callback_update("probe:act:1", from_user=user(5005), callback_id="press-42", update_id=900)
    text = message_update("/probe", from_user=user(5005), message_id=77, update_id=901)
    for data in (press, text, press, text):
        await feed(application, data)
    first_press, first_text, again_press, again_text = (command for _, command in probe.seen)
    assert first_press == again_press and first_text == again_text
    assert first_press != first_text


def test_command_ids_follow_the_update_identity():
    bot = None
    press = Update.de_json(callback_update("p:a", from_user=user(1), callback_id="abc"), bot)
    same_press_other_update = Update.de_json(callback_update("p:b", from_user=user(1), callback_id="abc"), bot)
    other_press = Update.de_json(callback_update("p:a", from_user=user(1), callback_id="abd"), bot)
    assert command_id(press) == command_id(same_press_other_update) != command_id(other_press)
    message = Update.de_json(message_update("x", from_user=user(1), chat_id=10, message_id=5), bot)
    other_chat = Update.de_json(message_update("x", from_user=user(1), chat_id=11, message_id=5), bot)
    assert command_id(message) != command_id(other_chat)
    assert command_id(message) == command_id(Update.de_json(
        message_update("changed", from_user=user(1), chat_id=10, message_id=5), bot))


def test_an_edited_message_is_no_command():
    edited = Update.de_json(message_update("/probe", from_user=user(1), edited=True), None)
    with pytest.raises(ValueError):
        command_id(edited)


async def test_an_edited_message_reaches_no_function(gateway, make_application):
    probe = Probe(gateway)
    application = await make_application(functions=(probe_function(probe),))
    await feed(application, message_update("/probe", from_user=user(5005), edited=True))
    assert probe.seen == []


# -- audience: an interface filter; the core decides the right ------------------------------

@pytest.mark.parametrize(("summary", "handled"), [
    ({"role": "owner", "status": "admitted", "ui_language": "ru"}, True),
    # The role alone is no right (QA C-ADAPTER): an owner profile awaiting admission.
    ({"role": "owner", "status": "pending", "ui_language": "ru"}, False),
    ({"role": "owner", "status": "deleted", "ui_language": "ru"}, False),
    ({"role": "user", "status": "admitted", "ui_language": "ru"}, False),
    (None, False),
])
async def test_owner_functions_reach_only_an_admitted_owner(telegram, gateway, make_application, summary, handled):
    gateway.summaries["5005"] = summary
    probe = Probe(gateway)
    application = await make_application(functions=(probe_function(probe, "owner"),))
    await feed(application, message_update("/probe", from_user=user(5005)))
    assert bool(probe.seen) is handled
    # Anyone the filter turns away gets the default answer, not silence.
    assert len(telegram.calls_to("sendMessage")) == (0 if handled else 1)


async def test_admitted_functions_need_an_admitted_profile(gateway, make_application):
    gateway.summaries["5005"] = {"role": "user", "status": "admitted", "ui_language": "ru"}
    probe = Probe(gateway)
    application = await make_application(functions=(probe_function(probe, "admitted"),))
    await feed(application, message_update("/probe", from_user=user(5005)))
    assert len(probe.seen) == 1


# -- discovery -----------------------------------------------------------------------------

@pytest.fixture
def service_package(tmp_path, monkeypatch):
    package = tmp_path / "probe_services"
    package.mkdir()
    (package / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield package
    for name in [name for name in sys.modules if name.startswith("probe_services")]:
        del sys.modules[name]


FUNCTION = textwrap.dedent("""
    from wortly_telegram.router import ServiceFunction
    async def handle(update, context):
        return None
    SERVICE_FUNCTIONS = (ServiceFunction("{name}", ("{command}",), None, "owner", handle),)
""")


def test_service_modules_are_found_and_must_declare_functions(service_package):
    (service_package / "inbox.py").write_text(FUNCTION.format(name="inbox", command="inbox"))
    (service_package / "_shared.py").write_text("X = 1\n")
    [function] = discover("probe_services")
    assert function.commands == ("inbox",)
    (service_package / "broken.py").write_text("X = 1\n")
    with pytest.raises(RouterError):
        discover("probe_services")


async def test_a_command_claimed_twice_stops_the_build(service_package, make_application):
    (service_package / "a.py").write_text(FUNCTION.format(name="a", command="same"))
    (service_package / "b.py").write_text(FUNCTION.format(name="b", command="same"))
    with pytest.raises(RouterError):
        await make_application(functions=discover("probe_services"))


async def test_start_cannot_be_taken_by_a_service_function(make_application):
    async def handle(update, context):
        return None
    with pytest.raises(RouterError):
        await make_application(functions=(ServiceFunction("x", ("start",), None, "anyone", handle),))


@pytest.mark.parametrize("arguments", [
    ("", ("a",), None, "anyone"),
    ("x", ("Start",), None, "anyone"),
    ("x", ["a"], None, "anyone"),
    ("x", ("a",), "has:colon", "anyone"),
    ("x", (), None, "anyone"),
    ("x", ("a",), None, "everyone"),
])
def test_service_function_declarations_are_checked(arguments):
    async def handle(update, context):
        return None
    with pytest.raises(ValueError):
        ServiceFunction(*arguments, handle)
