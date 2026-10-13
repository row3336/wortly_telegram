"""The bot's side of the user-path trail (R3-03; events TZ §4.4, scenario V5, §5 and root decisions of 13.10).

No database and no socket: the real router and gateway run over the fake Bot API and the fake
core (httpx.MockTransport). A capture handler on the trail's own logger receives the lines
as a process's stream would. The negative probes at the end feed marker values (chat, user
name, text, button data, request ID) through the whole path and look for them, and for the
values derived from them, in every line.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from uuid import UUID, uuid4

import httpx
import pytest
from telegram import Update

from conftest import feed
from fakes.core_gateway import CORE_SECRET, CORE_URL, FakeCore, core_settings
from fakes.telegram_api import callback_update, message_update, user
from wortly_telegram import trail as bot_trail
from wortly_telegram.gateway import CoreError, CoreUnavailable, build_gateway, principal_of
from wortly_telegram.router import COMMAND_NAMESPACE, ServiceFunction, command_id
from wortly_telegram.service.access import ReasonInputs, service_functions
from wortly.identity.principal import TelegramPrincipal
from wortly.operations import trail as core_trail


OWNER = {"role": "owner", "status": "admitted", "ui_language": "ru"}
HEADER = "x-wortly-trace-id"
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


@pytest.fixture
def core() -> FakeCore:
    return FakeCore()


@pytest.fixture
async def gateway(core):
    """The real gateway over the fake core: the router's calls become requests the fake core records."""
    current = build_gateway(core_settings(), transport=core.transport())
    yield current
    await current.aclose()


def admitted_owner(core: FakeCore, times: int = 4) -> None:
    for _ in range(times):
        core.answer("/internal/v1/admission/summary", httpx.Response(200, json={"profile": OWNER}))


def trace_headers(core: FakeCore) -> list[tuple[str, str]]:
    return [(request.url.path, request.headers.get(HEADER)) for request in core.requests]


# -- V5: one chain by one trace, from the update to the core and the reply ----------------------

async def test_an_owners_decision_is_one_chain_of_one_trace_in_the_header_and_in_every_line(
        make_application, core, trail_lines):
    admitted_owner(core)
    application = await make_application(functions=service_functions(ReasonInputs()))
    await feed(application, callback_update(f"acc:a:{uuid4()}:3", from_user=user(5005), callback_id="901"))

    lines = trail_lines.parsed
    traces = {line["trace"] for line in lines}
    assert len(traces) == 1 and UUID4.fullmatch(traces.pop())
    assert {path for path, _ in trace_headers(core)} == {"/internal/v1/contact", "/internal/v1/admission/summary",
                                                         "/internal/v1/admission/decide"}
    assert {header for _, header in trace_headers(core)} == {lines[0]["trace"]}
    assert [line["ev"] for line in lines].count("b.core") == len(core.requests)
    [update] = trail_lines.events("b.update")
    assert (update["kind"], update["fn"], update["outcome"], update["comp"]) == ("callback", "access", "handled", "bot")
    assert isinstance(update["ms"], int) and update["ms"] >= 0
    # The update's line closes the chain: the calls and the replies were written while it was handled.
    assert lines[-1]["ev"] == "b.update"
    assert {line["outcome"] for line in trail_lines.events("b.reply")} == {"sent"}
    decide = [line for line in trail_lines.events("b.core") if line["route"].endswith("/admission/decide")]
    assert [(line["status"], line.get("code")) for line in decide] == [(200, None)]


async def test_each_update_gets_its_own_random_trace(make_application, core, trail_lines):
    admitted_owner(core)
    application = await make_application(functions=service_functions(ReasonInputs()))
    await feed(application, message_update("hello", from_user=user(5005)))
    await feed(application, message_update("hello again", from_user=user(5005), message_id=2))
    first, second = trail_lines.events("b.update")
    assert first["trace"] != second["trace"]
    assert all(UUID4.fullmatch(line["trace"]) for line in trail_lines.parsed)
    # Every request of an update carries that update's trace, none carries the other's.
    assert {header for _, header in trace_headers(core)} == {first["trace"], second["trace"]}


def in_a_group(data: dict) -> dict:
    data["callback_query"]["message"]["chat"].update(id=-1001, type="group")
    return data


@pytest.mark.parametrize(("make", "kind", "fn", "outcome"), [
    (lambda: message_update("hello", from_user=user(5005)), "message", "default", "handled"),
    (lambda: message_update("/start", from_user=user(5005)), "message", "start", "handled"),
    (lambda: callback_update("old:button:1", from_user=user(5005)), "callback", "default", "handled"),
    (lambda: in_a_group(callback_update(f"acc:a:{uuid4()}:1", from_user=user(5005))), "callback", "access", "ignored"),
], ids=["text", "start", "old-button", "owner-button-in-a-group"])
async def test_the_update_line_names_the_kind_the_function_and_the_outcome(
        make_application, gateway, trail_lines, make, kind, fn, outcome):
    application = await make_application(functions=service_functions(ReasonInputs()))
    await feed(application, make())
    [update] = trail_lines.events("b.update")
    assert (update["kind"], update["fn"], update["outcome"]) == (kind, fn, outcome)


async def test_fn_is_the_closed_list_of_the_routers_functions_and_nothing_else(make_application, gateway, trail_lines):
    application = await make_application(functions=service_functions(ReasonInputs()))
    assert {"start", "access", "default"} <= core_trail.FUNCTIONS
    # A name that looks like a user name or a word is not on the list, so the writer drops it.
    bot_trail.emit("b.update", component="bot", trace=str(uuid4()), kind="message", fn="ivan_petrov",
                   outcome="handled")
    [line] = trail_lines.parsed
    assert "fn" not in line and line["dropped"] == 1 and "ivan_petrov" not in trail_lines.everything
    await feed(application, message_update("/start", from_user=user(5005)))
    assert trail_lines.events("b.update")[-1]["fn"] == "start"


async def test_a_button_of_a_user_who_is_not_the_owner_is_denied(make_application, core, trail_lines):
    core.answer("/internal/v1/admission/summary",
                httpx.Response(200, json={"profile": {"role": "user", "status": "admitted", "ui_language": "ru"}}))
    application = await make_application(functions=service_functions(ReasonInputs()))
    await feed(application, callback_update(f"acc:a:{uuid4()}:1", from_user=user(6006)))
    [update] = trail_lines.events("b.update")
    assert (update["fn"], update["outcome"]) == ("access", "denied")
    assert not [path for path in core.paths() if path.endswith("/decide")]


async def test_a_handler_that_raises_is_a_failed_update_with_the_class_only(make_application, gateway, trail_lines):
    async def handle(update, context):
        raise ValueError("Anna wrote a secret sentence")

    application = await make_application(functions=[ServiceFunction("boom", ("boom",), None, "anyone", handle)])
    await feed(application, message_update("/boom", from_user=user(5005)))
    [update] = trail_lines.events("b.update")
    assert (update["outcome"], update["err"], update["lvl"], update["fn"]) == ("failed", "ValueError", "warning", "boom")
    assert "secret" not in trail_lines.everything and "Anna" not in trail_lines.everything


async def test_a_text_claimed_by_a_function_is_its_update(make_application, core, trail_lines):
    admitted_owner(core, 8)
    inputs = ReasonInputs()
    application = await make_application(functions=service_functions(inputs))
    request = uuid4()
    await feed(application, callback_update(f"acc:w:{request}:2", from_user=user(5005), callback_id="902"))
    await feed(application, message_update("not suitable", from_user=user(5005), message_id=9))
    first, second = trail_lines.events("b.update")
    assert (first["fn"], second["fn"]) == ("access", "access")
    assert "not suitable" not in trail_lines.everything


# -- b.core: status, the core's own code, milliseconds; the failures ----------------------------

PRINCIPAL = TelegramPrincipal("5005")


async def test_a_refusal_of_the_core_is_written_with_its_status_and_code_and_no_body(core, gateway, trail_lines):
    core.answer("/internal/v1/admission/pending_requests", httpx.Response(
        403, json={"error": {"code": "FORBIDDEN", "detail": "Anna Smith 5005 is not the owner"}}))
    with pytest.raises(CoreError):
        await gateway.pending_requests(PRINCIPAL)
    [line] = trail_lines.events("b.core")
    assert (line["route"], line["status"], line["code"], line["lvl"]) == (
        "/internal/v1/admission/pending_requests", 403, "FORBIDDEN", "info")
    assert "Anna" not in trail_lines.everything and "5005" not in trail_lines.everything


async def test_a_failing_core_is_a_warning_without_the_text_of_the_failure(core, gateway, trail_lines):
    core.answer("/internal/v1/contact", httpx.Response(500, text="Traceback ... postgresql://user:pw@host/db"))
    core.answer("/internal/v1/admission/summary", httpx.ConnectError("connect to 10.1.2.3:8001 failed", request=None))
    with pytest.raises(CoreUnavailable):
        await gateway.note_contact(PRINCIPAL)
    with pytest.raises(CoreUnavailable):
        await gateway.profile_summary(PRINCIPAL)
    contact, summary = trail_lines.events("b.core")
    assert (contact["status"], "code" in contact, contact["lvl"]) == (500, False, "warning")
    assert (summary["err"], "status" in summary, summary["lvl"]) == ("ConnectError", False, "warning")
    assert not any(marker in trail_lines.everything for marker in ("postgresql", "10.1.2.3", "Traceback", CORE_URL))


async def test_the_route_is_the_template_and_the_line_never_holds_the_secret(core, gateway, trail_lines):
    await gateway.profile_summary(PRINCIPAL)
    [line] = trail_lines.events("b.core")
    assert line["route"] == "/internal/v1/admission/summary"
    assert CORE_SECRET not in trail_lines.everything and "core.internal" not in trail_lines.everything
    assert set(line) == {"ts", "ev", "lvl", "ver", "comp", "trace", "route", "status", "ms"}


# -- calls outside an update: a trace of their own, the lease stays quiet -----------------------

async def test_calls_outside_an_update_each_get_a_fresh_trace(core, gateway, trail_lines):
    await gateway.note_contact(PRINCIPAL)
    await gateway.note_contact(PRINCIPAL)
    first, second = trail_lines.events("b.core")
    assert first["trace"] != second["trace"]
    assert [header for _, header in trace_headers(core)] == [first["trace"], second["trace"]]


async def test_the_successful_and_the_held_lease_calls_write_nothing_and_a_lost_one_does(core, gateway, trail_lines):
    from datetime import timedelta

    lease = await gateway.leases.acquire("bot", holder="bot-1", duration=timedelta(seconds=30))
    await gateway.leases.renew(lease, duration=timedelta(seconds=30))
    assert trail_lines.events("b.core") == []
    assert len(core.requests) == 2 and all(header for _, header in trace_headers(core))
    # A standby bot is refused every standby_retry: the normal state, no line (409 LEASE_HELD).
    other = await gateway.leases.acquire("bot", holder="bot-2", duration=timedelta(seconds=30))
    assert other is None
    assert trail_lines.events("b.core") == []
    # A lease lost is a failure and is written.
    core.lease = None
    with pytest.raises(CoreError):
        await gateway.leases.renew(lease, duration=timedelta(seconds=30))
    [line] = trail_lines.events("b.core")
    assert (line["route"], line["status"], line["code"]) == ("/internal/v1/leases/bot/renew", 409, "LEASE_LOST")


async def test_a_background_contact_retry_does_not_inherit_the_trace_of_its_update(
        make_application, core, gateway, trail_lines, sleeps):
    core.answer("/internal/v1/contact", httpx.ConnectError("down", request=None))
    application = await make_application()
    await feed(application, message_update("hello", from_user=user(5005)))
    for _ in range(100):  # the retry is a task of the loop; its pauses are the fake sleeps
        await asyncio.sleep(0)
    update_trace = trail_lines.events("b.update")[0]["trace"]
    contacts = [line for line in trail_lines.events("b.core") if line["route"].endswith("/contact")]
    assert contacts[0]["trace"] == update_trace
    later = contacts[1:]
    assert later, "the retry ran after the first failure"
    assert all(line["trace"] != update_trace for line in later)


# -- b.reply: sent, refused, unknown -------------------------------------------------------------

async def reply(telegram, sleeps):
    from wortly_telegram.reply import send_reply

    async with telegram.bot() as bot:
        return await send_reply(lambda: bot.send_message(chat_id=5005, text="Hello Anna"), sleep=sleeps)


@pytest.mark.parametrize(("script", "outcome", "err", "level"), [
    (lambda t: None, "sent", None, "info"),
    (lambda t: t.fail("sendMessage", 400, "Bad Request: chat 5005 not found"), "refused", "BadRequest", "warning"),
    (lambda t: t.raise_error("sendMessage", httpx.ReadTimeout("slow", request=None)), "unknown", "TimedOut", "warning"),
    (lambda t: t.fail("sendMessage", 500, "Internal Server Error"), "unknown", "NetworkError", "warning"),
], ids=["sent", "refused", "read-timeout", "500"])
async def test_a_reply_line_has_the_outcome_and_the_error_class_only(telegram, sleeps, trail_lines,
                                                                      script, outcome, err, level):
    script(telegram)
    trace = bot_trail.begin_update()
    await reply(telegram, sleeps)
    [line] = trail_lines.events("b.reply")
    assert (line["outcome"], line.get("err"), line["lvl"], line["trace"]) == (outcome, err, level, trace)
    assert set(line) - {"err"} == {"ts", "ev", "lvl", "ver", "comp", "trace", "outcome"}
    assert "Anna" not in trail_lines.everything and "5005" not in trail_lines.everything


async def test_repeats_that_ran_out_are_one_refused_line_and_the_error_goes_up(telegram, sleeps, trail_lines):
    from telegram.error import RetryAfter

    from wortly_telegram.reply import MAX_REPEATS

    for _ in range(MAX_REPEATS + 1):
        telegram.fail("sendMessage", 429, "Too Many Requests", retry_after=1)
    with pytest.raises(RetryAfter):
        await reply(telegram, sleeps)
    [line] = trail_lines.events("b.reply")
    assert (line["outcome"], line["err"]) == ("refused", "RetryAfter")


async def test_a_reply_without_an_update_has_no_trace_but_is_written(telegram, sleeps, trail_lines):
    bot_trail.end_update()
    await reply(telegram, sleeps)
    [line] = trail_lines.events("b.reply")
    assert "trace" not in line and line["outcome"] == "sent"


# -- the writer of the adapter -------------------------------------------------------------------

def test_emit_passes_no_forbidden_field_and_writes_no_unknown_one(trail_lines):
    bot_trail.allow_functions(["start"])
    bot_trail.emit("b.update", component="bot", trace=str(uuid4()), kind="message", fn="start", outcome="handled",
                   cmd=uuid4(), chat_id=5005, telegram_user_id="5005", username="anna_k", first_name="Anna",
                   text="Hello", callback_data="acc:a:1", logical_key="k", snapshot="s", prof=uuid4(), areq=uuid4())
    [line] = trail_lines.parsed
    assert set(line) == {"ts", "ev", "lvl", "ver", "comp", "trace", "kind", "fn", "outcome"}
    assert "dropped" not in line, "forbidden fields are not even offered to the writer"


def test_the_writer_itself_refuses_what_the_adapter_never_sends(trail_lines):
    core_trail.trail("b.update", comp="bot", chat_id=5005, text="Hello", callback_data="acc:a:1", cmd="not a uuid",
                     fn="Free Text", kind="a sentence", outcome="made up")
    [line] = trail_lines.parsed
    assert set(line) == {"ts", "ev", "lvl", "ver", "comp", "dropped"} and line["dropped"] == 7


def test_the_forbidden_names_cover_what_the_decisions_of_13_10_list():
    assert {"cmd", "chat_id", "telegram_user_id", "username", "first_name", "text", "callback_data", "logical_key",
            "snapshot"} <= bot_trail.FORBIDDEN_FIELDS
    # None of them is a field of the writer's table either, except the profile-bound ones the bot never has.
    assert not ({"chat_id", "telegram_user_id", "username", "first_name", "text", "callback_data", "logical_key",
                 "snapshot"} & set(core_trail.FIELDS))


def test_the_process_entry_gives_the_trail_its_own_stream_with_the_component_and_the_build_version(capsys):
    bot_trail.allow_functions(["start"])
    with bot_trail.process_trail("bot"):
        bot_trail.emit("b.update", component="bot", trace=str(uuid4()), kind="message", fn="start", outcome="handled")
    [line] = [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.startswith("{")]
    assert (line["ev"], line["comp"]) == ("b.update", "bot")
    assert re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", line["ver"]) and line["ver"] == bot_trail.build_version()


# -- negative probes: no personal value, no derived value, in any line of the whole path --------

CHAT = 7_700_123_456
SENDER = user(CHAT, first_name="Marlene", last_name="Zuckerberger", username="marker_username_q", language_code="de")
TEXT = "MarkerSentenceQ Katze"
REQUEST = uuid4()
CALLBACK_IDS = ("cbq-marker-9001", "cbq-marker-9002")
MESSAGE_IDS = (11, 12, 13)


def _derived() -> set[str]:
    """The values that lead back to the markers without a database (events TZ V1, P2-3)."""
    values: set[str] = set()
    for message_id in range(1, 40):
        derived = command_id_of(f"msg:{CHAT}:{message_id}")
        values |= {derived, derived.replace("-", "")}
    for callback_id in CALLBACK_IDS:
        derived = command_id_of(f"cbq:{callback_id}")
        values |= {derived, derived.replace("-", "")}
    for marker in (str(CHAT), SENDER["username"], SENDER["first_name"], SENDER["last_name"], TEXT, str(REQUEST)):
        values |= {hashlib.sha256(marker.encode()).hexdigest(), hashlib.sha256(marker.encode()).hexdigest()[:32]}
    return values


def command_id_of(text: str) -> str:
    from uuid import uuid5

    return str(uuid5(COMMAND_NAMESPACE, text))


async def test_no_marker_and_no_derived_value_appears_in_any_line_of_the_whole_path(
        make_application, core, trail_lines, telegram):
    admitted_owner(core, 12)
    application = await make_application(functions=service_functions(ReasonInputs()))
    updates = [
        message_update("/start", from_user=SENDER, message_id=MESSAGE_IDS[0]),
        message_update(TEXT, from_user=SENDER, message_id=MESSAGE_IDS[1]),
        callback_update(f"acc:a:{REQUEST}:3", from_user=SENDER, callback_id=CALLBACK_IDS[0]),
        callback_update(f"acc:w:{REQUEST}:3", from_user=SENDER, callback_id=CALLBACK_IDS[1]),
        message_update("Reason Marker Text", from_user=SENDER, message_id=MESSAGE_IDS[2]),
    ]
    for data in updates:
        await feed(application, data)
    # The probe is only meaningful when the derived command IDs really are the ones the bot computed.
    sent = {str(command_id(Update.de_json(data, application.bot))) for data in updates}
    assert sent <= _derived()
    text = trail_lines.everything
    assert trail_lines.events("b.update") and trail_lines.events("b.core") and trail_lines.events("b.reply")
    for marker in _derived() | {str(CHAT), SENDER["username"], SENDER["first_name"], SENDER["last_name"], TEXT,
                                "Reason Marker Text", str(REQUEST), REQUEST.hex, CORE_SECRET, "core.internal",
                                "acc:a", "acc:w", "callback_data", "chat_id", "telegram_user_id", "logical_key",
                                "snapshot", "command_id", '"cmd"', '"prof"', '"areq"', '"sess"', '"inst"', '"doc"',
                                "initData", "token"}:
        assert marker not in text, marker
    # Every line holds only the fields of its event (§4.4) and nothing the writer had to drop.
    allowed = {"b.update": {"trace", "kind", "fn", "outcome", "ms", "err"},
               "b.core": {"trace", "route", "status", "code", "ms", "err"},
               "b.reply": {"trace", "outcome", "err"}}
    common = {"ts", "ev", "lvl", "ver", "comp"}
    for line in trail_lines.parsed:
        assert set(line) <= common | allowed[line["ev"]], line
        assert line["comp"] == "bot" and "dropped" not in line and "truncated" not in line


async def test_the_bot_command_id_is_not_written_in_any_event_even_for_the_decision(make_application, core,
                                                                                    trail_lines):
    admitted_owner(core)
    application = await make_application(functions=service_functions(ReasonInputs()))
    data = callback_update(f"acc:a:{uuid4()}:3", from_user=SENDER, callback_id=CALLBACK_IDS[0])
    await feed(application, data)
    [decide] = core.bodies("/internal/v1/admission/decide")
    assert decide["command_id"] == str(command_id(Update.de_json(data, application.bot)))
    text = trail_lines.everything
    assert decide["command_id"] not in text and decide["command_id"].replace("-", "") not in text
    assert decide["actor_telegram_id"] not in text


def test_the_probe_would_see_a_leak_if_the_adapter_guard_were_gone(monkeypatch, trail_lines):
    bot_trail.allow_functions(["start"])
    # Positive control for the probes above: without the adapter's guard the writer takes a known field
    # (cmd is one), so only that guard keeps the bot's derived command ID out of the line.
    monkeypatch.setattr(bot_trail, "FORBIDDEN_FIELDS", frozenset())
    leaked = command_id_of(f"msg:{CHAT}:{MESSAGE_IDS[0]}")
    bot_trail.emit("b.update", component="bot", trace=str(uuid4()), kind="message", fn="start", outcome="handled",
                   cmd=leaked)
    assert leaked in trail_lines.everything and leaked in _derived()


async def test_without_a_configured_stream_the_trail_writes_nothing_anywhere(make_application, core, capsys):
    # The adapter's tests and tools that do not call process_trail see no output at all.
    admitted_owner(core)
    application = await make_application(functions=service_functions(ReasonInputs()))
    await feed(application, message_update("hello", from_user=SENDER))
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""


def test_the_trace_made_for_an_update_is_a_random_uuid4_that_is_not_derived_from_anything():
    first, second = bot_trail.begin_update(), bot_trail.begin_update()
    assert first != second and UUID4.fullmatch(first) and UUID(first).version == 4
    assert bot_trail.update_trace() == second
    bot_trail.end_update()
    assert bot_trail.update_trace() is None
    assert bot_trail.call_trace() != bot_trail.call_trace()


def test_the_principal_of_an_update_is_unchanged_by_the_trail():
    update = Update.de_json(message_update("hi", from_user=SENDER), None)
    assert principal_of(update) == TelegramPrincipal(str(CHAT))


# -- the process entries give the trail its own stream, inside the cleaning of private_logs ---------

def test_the_bot_entry_runs_inside_the_trail_stream_and_not_through_the_root_handler(monkeypatch, capsys, tmp_path):
    bot_trail.allow_functions(["start"])
    import logging
    from types import SimpleNamespace

    from wortly_telegram import bot_main, config

    settings = SimpleNamespace(telegram=SimpleNamespace(bot_token="123456:SYNTHETIC-TOKEN-FAKE"),
                               core=SimpleNamespace(service_secret=CORE_SECRET))
    monkeypatch.setattr(config, "load_bot_settings", lambda path: settings)

    def serve(_settings):
        bot_trail.emit("b.update", component="bot", trace=str(uuid4()), kind="message", fn="start",
                       outcome="handled", ms=1)
        return 0

    monkeypatch.setattr(bot_main, "serve", serve)
    root = logging.getLogger()
    root_stream = io.StringIO()
    root_handler = logging.StreamHandler(root_stream)
    root.addHandler(root_handler)
    try:
        assert bot_main.main(["--config", str(tmp_path / "bot.toml")]) == 0
    finally:
        root.removeHandler(root_handler)
    lines = [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.startswith("{")]
    assert [(line["ev"], line["comp"]) for line in lines] == [("b.update", "bot")]
    assert "b.update" not in root_stream.getvalue(), "the trail does not propagate to the root's handlers"
    # After the entry the stream is gone: a later line goes nowhere.
    bot_trail.emit("b.update", component="bot", trace=str(uuid4()), kind="message", fn="start", outcome="handled")
    assert capsys.readouterr().err == ""


def test_the_delivery_entry_runs_inside_the_trail_stream(monkeypatch, capsys, tmp_path):
    pytest.importorskip("psycopg")
    from types import SimpleNamespace

    from wortly_telegram import config, logs
    from wortly_telegram.delivery import main as delivery_main

    settings = SimpleNamespace(telegram=SimpleNamespace(bot_token="123456:SYNTHETIC-TOKEN-FAKE"), database=None)
    monkeypatch.setattr(config, "load_delivery_settings", lambda path: settings)
    monkeypatch.setattr(logs, "log_secrets", lambda token, database: (token,))

    def serve(_settings):
        bot_trail.emit("d.job.done", component="delivery", job=uuid4(), att=uuid4(), kind="access_approved",
                       outcome="sent", attempt=1, ms=1)
        return 0

    monkeypatch.setattr(delivery_main, "serve", serve)
    assert delivery_main.main(["--config", str(tmp_path / "delivery.toml")]) == 0
    lines = [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.startswith("{")]
    assert [(line["ev"], line["comp"]) for line in lines] == [("d.job.done", "delivery")]
