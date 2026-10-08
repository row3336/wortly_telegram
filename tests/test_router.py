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
from fakes.telegram_api import WEB_APP_URL, callback_update, message_update, my_chat_member_update, user
from wortly_telegram import router
from wortly_telegram.gateway import CoreError, CoreUnavailable, principal_of
from wortly_telegram.router import (
    BotContext, RouterError, ServiceFunction, command_id, discover, note_contact,
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

    async def note_contact(self, principal, *, age_ms: int = 0) -> None:
        self.events.append(("contact", principal.external_id))
        if self.contact_error is not None:
            self.calls.append(GatewayCall("note_contact", principal, {"age_ms": age_ms} if age_ms else {}))
            raise self.contact_error
        await super().note_contact(principal, age_ms=age_ms)


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


async def test_an_unblock_event_is_one_contact_and_gets_no_answer(telegram, gateway, make_application, caplog):
    # Я18: Telegram's report that the user unblocked the bot lifts the mark without a new message.
    gateway.blocked = {"5005"}
    probe = Probe(gateway)
    application = await make_application(functions=(probe_function(probe),))
    with caplog.at_level(logging.INFO):
        await feed(application, my_chat_member_update("member", from_user=user(5005, first_name="Ivanna")))
    assert contacts(gateway) == ["5005"]
    assert gateway.blocked == set()
    # An event is not a message: no answer, no service function, no command.
    assert telegram.calls == [call for call in telegram.calls if call.method == "getMe"]
    assert probe.seen == []
    logged = [record.getMessage() for record in caplog.records]
    assert "unblock event received" in logged
    assert not any("5005" in line or "Ivanna" in line for line in logged)


async def test_repeated_unblock_events_are_repeated_idempotent_contacts(gateway, make_application):
    gateway.blocked = {"5005"}
    application = await make_application()
    event = my_chat_member_update("member", from_user=user(5005), update_id=9001)
    await feed(application, event)
    await feed(application, event)  # the same update twice (two pollers, a restart)
    await feed(application, message_update("hello", from_user=user(5005)))
    assert contacts(gateway) == ["5005", "5005", "5005"]
    assert gateway.blocked == set()


@pytest.mark.parametrize("make", [
    # A block is no contact: the mark comes only from a delivery refused as blocked (answer 2.5).
    lambda: my_chat_member_update("kicked", from_user=user(5005), old_status="member"),
    # Not the user's own change of a private chat: someone else, or a group.
    lambda: my_chat_member_update("member", from_user=user(6006), chat_id=5005),
    lambda: my_chat_member_update("member", from_user=user(5005), chat_id=-1001, chat_type="group",
                                  old_status="left"),
], ids=["block", "other-user", "group"])
async def test_other_membership_updates_are_no_contact(telegram, gateway, make_application, make):
    gateway.blocked = {"5005"}
    application = await make_application()
    await feed(application, make())
    assert contacts(gateway) == []
    assert gateway.blocked == {"5005"}
    assert telegram.calls == [call for call in telegram.calls if call.method == "getMe"]


async def test_a_failing_unblock_contact_is_logged_by_class_only(gateway, make_application, caplog):
    gateway.contact_error = RuntimeError("host=db.internal user 5005")
    application = await make_application()
    with caplog.at_level(logging.DEBUG):
        await feed(application, my_chat_member_update("member", from_user=user(5005)))
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "note_contact failed: RuntimeError" in logged
    assert "db.internal" not in logged and "5005" not in logged


def test_the_poller_asks_for_the_unblock_update():
    from wortly_telegram.router import ALLOWED_UPDATES
    assert ALLOWED_UPDATES == ("message", "callback_query", "my_chat_member")


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

    async def hang(principal, **_) -> None:
        await asyncio.sleep(30)

    gateway.note_contact = hang
    monkeypatch.setattr(router, "CONTACT_TIMEOUT", 0.05)
    application = await make_application()
    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(feed(application, message_update("hello", from_user=user(5005))), timeout=5)
    assert len(telegram.calls_to("sendMessage")) == 1
    assert "note_contact failed: TimeoutError" in " ".join(r.getMessage() for r in caplog.records)


# -- a contact the core did not take is retried in the background (PP31, finding 1.8) ----

class FlakyGateway(FakeCoreGateway):
    """The core gives no answer to the first ``failures`` contacts, then takes them."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        super().__init__()
        self.failures = failures
        self.failure = error or CoreUnavailable()

    async def note_contact(self, principal, *, age_ms: int = 0) -> None:
        if self.failures:
            self.failures -= 1
            self.calls.append(GatewayCall("note_contact", principal, {"age_ms": age_ms}))
            raise self.failure
        await super().note_contact(principal, age_ms=age_ms)
        self.calls[-1].arguments.setdefault("age_ms", age_ms)


class Clock:
    """A monotonic clock that the pauses move forward."""

    def __init__(self) -> None:
        self.now = 100.0
        self.pauses: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.now += seconds + 0.25  # each retry's own call takes a little time too


def bot_context(telegram, gateway, clock) -> BotContext:
    return BotContext(bot=telegram.bot(), gateway=gateway, web_app_url=WEB_APP_URL, sleep=clock.sleep,
                      monotonic=clock)


def an_update(telegram, data) -> Update:
    return Update.de_json(data, telegram.bot())


async def retries_done(context: BotContext) -> None:
    await asyncio.gather(*context.contact_retries.values())


def ages(gateway) -> list[int]:
    return [call.arguments.get("age_ms", 0) for call in gateway.calls if call.operation == "note_contact"]


@pytest.mark.parametrize("error", [CoreUnavailable(), TimeoutError()], ids=["unavailable", "timeout"])
async def test_an_unanswered_contact_is_retried_with_its_age_until_the_core_takes_it(telegram, caplog, error):
    gateway, clock = FlakyGateway(2, error), Clock()
    gateway.blocked = {"5005"}
    context = bot_context(telegram, gateway, clock)
    with caplog.at_level(logging.INFO):
        await note_contact(an_update(telegram, my_chat_member_update("member", from_user=user(5005))), context)
        assert list(context.contact_retries) == ["5005"]   # the handler did not wait for the retries
        await retries_done(context)
    assert clock.pauses == [2.0, 10.0]
    # The age is the time since the update, so the core dates the contact back.
    assert ages(gateway) == [0, 2250, 12500]
    assert gateway.blocked == set() and context.contact_retries == {}
    logged = [record.getMessage() for record in caplog.records]
    assert "note_contact retried: done" in logged
    assert not any("5005" in line for line in logged)


async def test_retries_end_after_the_last_pause(telegram, caplog):
    gateway, clock = FlakyGateway(100), Clock()
    context = bot_context(telegram, gateway, clock)
    with caplog.at_level(logging.INFO):
        await note_contact(an_update(telegram, message_update("hello", from_user=user(5005))), context)
        await retries_done(context)
    assert clock.pauses == list(router.CONTACT_RETRY_DELAYS)
    assert len(ages(gateway)) == 1 + len(router.CONTACT_RETRY_DELAYS)
    assert context.contact_retries == {}
    assert "note_contact given up after the last retry" in [record.getMessage() for record in caplog.records]


async def test_a_refusal_of_the_core_is_not_retried(telegram):
    gateway, clock = FlakyGateway(1, CoreError("FORBIDDEN", status=403)), Clock()
    context = bot_context(telegram, gateway, clock)
    await note_contact(an_update(telegram, message_update("hello", from_user=user(5005))), context)
    assert context.contact_retries == {} and clock.pauses == [] and ages(gateway) == [0]


async def test_a_retry_stops_at_a_refusal(telegram):
    gateway, clock = FlakyGateway(1), Clock()
    context = bot_context(telegram, gateway, clock)
    await note_contact(an_update(telegram, message_update("hello", from_user=user(5005))), context)
    gateway.failures, gateway.failure = 1, CoreError("FORBIDDEN", status=403)
    await retries_done(context)
    assert clock.pauses == [2.0] and len(ages(gateway)) == 2


async def test_one_retry_per_user_at_a_time(telegram):
    gateway, clock = FlakyGateway(3), Clock()
    context = bot_context(telegram, gateway, clock)
    gate = asyncio.Event()

    async def held(seconds: float) -> None:
        await gate.wait()
        await clock.sleep(seconds)
    context = BotContext(bot=context.bot, gateway=gateway, web_app_url=WEB_APP_URL, sleep=held, monotonic=clock)
    await note_contact(an_update(telegram, message_update("one", from_user=user(5005))), context)
    first = context.contact_retries["5005"]
    await note_contact(an_update(telegram, message_update("two", from_user=user(5005), message_id=2)), context)
    await note_contact(an_update(telegram, message_update("hi", from_user=user(6006))), context)
    assert context.contact_retries["5005"] is first and set(context.contact_retries) == {"5005", "6006"}
    gate.set()
    await retries_done(context)
    assert context.contact_retries == {}


async def test_a_newer_unanswered_contact_moves_the_retrys_age_to_itself(telegram):
    # QA Н2 (probe b1): the retry is dated from the newest contact, not the first one.
    gateway, clock = FlakyGateway(2), Clock()
    gate = asyncio.Event()

    async def held(seconds: float) -> None:
        await gate.wait()
        await clock.sleep(seconds)
    context = BotContext(bot=telegram.bot(), gateway=gateway, web_app_url=WEB_APP_URL, sleep=held, monotonic=clock)
    await note_contact(an_update(telegram, message_update("one", from_user=user(5005))), context)   # t=100
    first = context.contact_retries["5005"]
    clock.now += 50.0
    await note_contact(an_update(telegram, message_update("two", from_user=user(5005), message_id=2)),
                       context)                                                                      # t=150
    assert context.contact_retries["5005"] is first
    gate.set()
    await retries_done(context)
    # The retry went at t=152.25: 2250 ms after the newest contact (52250 before the fix).
    assert ages(gateway) == [0, 0, 2250]
    assert context.contact_retries == {} and context.contact_seen == {}


async def test_an_older_time_never_moves_the_retrys_age_back(telegram):
    gateway, clock = FlakyGateway(2), Clock()
    gate = asyncio.Event()

    async def held(seconds: float) -> None:
        await gate.wait()
        await clock.sleep(seconds)
    context = BotContext(bot=telegram.bot(), gateway=gateway, web_app_url=WEB_APP_URL, sleep=held, monotonic=clock)
    await note_contact(an_update(telegram, message_update("one", from_user=user(5005))), context)   # t=100
    clock.now = 90.0  # a clock reading that is not later: the newest stays
    await note_contact(an_update(telegram, message_update("two", from_user=user(5005), message_id=2)), context)
    assert context.contact_seen == {"5005": 100.0}
    gate.set()
    await retries_done(context)


async def test_a_contact_that_fails_during_a_successful_retry_call_is_noted_too(telegram):
    # QA Д2 (probe b7): the retry's call is in flight with an age from t=100; the user writes
    # again at t=150 and that live contact fails too. When the call succeeds, the newer
    # contact is sent at once, with its own age, instead of ending the series.
    clock = Clock()
    release, in_call = asyncio.Event(), asyncio.Event()

    class SlowRetry(FlakyGateway):
        async def note_contact(self, principal, *, age_ms: int = 0) -> None:
            if age_ms == 2250:                 # the first retry's call: hold it open
                in_call.set()
                await release.wait()
            await super().note_contact(principal, age_ms=age_ms)

    gateway = SlowRetry(1)
    context = bot_context(telegram, gateway, clock)
    await note_contact(an_update(telegram, message_update("one", from_user=user(5005))), context)  # t=100
    await asyncio.wait_for(in_call.wait(), 5)
    clock.now = 160.0
    gateway.failures = 1
    await note_contact(an_update(telegram, message_update("two", from_user=user(5005), message_id=2)), context)
    assert context.contact_seen["5005"] == 160.0
    clock.now = 163.0
    release.set()
    await retries_done(context)
    assert context.contact_retries == {} and context.contact_seen == {}
    # failed t=100, failed t=160, the held retry (age from t=100), then at once the newer
    # contact with its age from t=160; no extra pause.
    assert ages(gateway) == [0, 0, 2250, 3000]
    assert clock.pauses == [2.0]


async def test_a_newer_contact_after_the_last_place_of_the_series_ends_it(telegram, monkeypatch):
    # The extra call takes a place of the same bounded series: no place left, no call.
    monkeypatch.setattr(router, "CONTACT_RETRY_DELAYS", (2.0,))
    clock = Clock()
    release, in_call = asyncio.Event(), asyncio.Event()

    class SlowRetry(FlakyGateway):
        async def note_contact(self, principal, *, age_ms: int = 0) -> None:
            if age_ms:
                in_call.set()
                await release.wait()
            await super().note_contact(principal, age_ms=age_ms)

    gateway = SlowRetry(1)
    context = bot_context(telegram, gateway, clock)
    await note_contact(an_update(telegram, message_update("one", from_user=user(5005))), context)
    await asyncio.wait_for(in_call.wait(), 5)
    clock.now = 160.0
    gateway.failures = 1
    await note_contact(an_update(telegram, message_update("two", from_user=user(5005), message_id=2)), context)
    release.set()
    await retries_done(context)
    assert ages(gateway) == [0, 0, 2250] and context.contact_retries == {}


async def test_the_age_never_exceeds_the_cores_bound(telegram):
    gateway, clock = FlakyGateway(1), Clock()
    context = bot_context(telegram, gateway, clock)

    async def stalled(seconds: float) -> None:
        clock.now += 3600  # a stalled process: far longer than the retries plan
    context = BotContext(bot=context.bot, gateway=gateway, web_app_url=WEB_APP_URL, sleep=stalled, monotonic=clock)
    await note_contact(an_update(telegram, message_update("hello", from_user=user(5005))), context)
    await retries_done(context)
    assert ages(gateway) == [0, router.CONTACT_AGE_MAX_MS]


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
