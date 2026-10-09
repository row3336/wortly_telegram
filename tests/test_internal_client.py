"""The client of the core's internal API: secret, paths, deadlines, refusals, the lease routes (answer 2.4).

Unit tests run on FakeCore behind httpx.MockTransport. The integration tests at the end
call the core's real internal application (wortly.api.internal.create_internal_app)
through httpx.ASGITransport on PostgreSQL wortly_test of the core's password-protected
stand (pg_stand); they need fastapi and psycopg, so they run in the core's interpreter
only (WORK_QUEUE §1, two interpreters). No test opens a socket: every IP connect and
name lookup fails (forbid_network).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import time
from uuid import uuid4

import httpx
import pytest

from fakes.core_gateway import CORE_SECRET, CORE_URL, FakeCore, core_settings, envelope, forbid_network
from wortly.identity.principal import TelegramPrincipal
from wortly_telegram.gateway import (
    SERVICE_SECRET_HEADER, CoreError, CoreUnavailable, HeldLease, HttpLeaseStore, InternalClient, build_gateway,
)


OWNER = TelegramPrincipal("1001")
L = timedelta(seconds=30)
ROUTES = ("/internal/v1/admission/summary", "/internal/v1/admission/decide", "/internal/v1/admission/change_mode",
          "/internal/v1/contact", "/internal/v1/leases/bot/acquire", "/internal/v1/leases/bot/renew",
          "/internal/v1/leases/bot/release")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


@pytest.fixture
def core() -> FakeCore:
    return FakeCore()


@pytest.fixture
async def gateway(core):
    current = build_gateway(core_settings(), transport=core.transport())
    yield current
    await current.aclose()


async def every_call(gateway) -> None:
    """Each route once, as the bot uses them."""
    await gateway.profile_summary(OWNER)
    await gateway.decide(OWNER, command_id=uuid4(), request_id=uuid4(), expected_version=1, approve=True,
                         reason=None)
    await gateway.change_mode(OWNER, command_id=uuid4(), expected_version=1, mode="automatic")
    await gateway.note_contact(OWNER)
    lease = await gateway.leases.acquire("bot", holder="bot-1", duration=L)
    lease = await gateway.leases.renew(lease, duration=L)
    assert await gateway.leases.release(lease) is True


async def test_every_call_is_one_post_with_the_secret_in_one_header_and_an_exact_path(core, gateway):
    await every_call(gateway)
    assert core.paths() == list(ROUTES)
    for request in core.requests:
        assert request.method == "POST"
        assert str(request.url) == CORE_URL + request.url.path
        assert not request.url.path.endswith("/") and not request.url.query
        assert request.headers.get_list(SERVICE_SECRET_HEADER) == [CORE_SECRET]
        assert request.headers["content-type"] == "application/json"
        # The secret is nowhere but in its header.
        assert CORE_SECRET not in str(request.url) and CORE_SECRET.encode() not in request.content
        assert [name for name, value in request.headers.multi_items()
                if CORE_SECRET in value] == [SERVICE_SECRET_HEADER.lower()]
    # Not a default header of the client either: httpx would show it in its own headers.
    assert not [value for value in gateway.client._client.headers.values() if CORE_SECRET in value]


async def test_the_secret_stays_out_of_reprs_errors_and_logs(core, gateway, caplog):
    caplog.set_level(logging.DEBUG)
    core.answer("/internal/v1/admission/summary", httpx.ConnectError(f"failed {CORE_URL}"), envelope(403, "FORBIDDEN"))
    for _ in range(2):
        with pytest.raises(CoreError) as raised:
            await gateway.profile_summary(OWNER)
        error = raised.value
        texts = [str(error), repr(error), *map(str, error.args), repr(error.__cause__)]
        assert not [text for text in texts if CORE_SECRET in text]
        # No chained transport error either: its text may name the address.
        assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)
    await every_call(gateway)
    seen = [repr(gateway), repr(gateway.client), repr(gateway.leases), repr(core_settings()),
            *(record.getMessage() for record in caplog.records), caplog.text]
    assert caplog.records, "httpx logged the requests at DEBUG"
    assert not [text for text in seen if CORE_SECRET in text]


async def test_redirects_are_not_followed_and_proxies_of_the_environment_are_ignored(core, gateway, monkeypatch):
    core.answer("/internal/v1/contact", httpx.Response(307, headers={"location": "http://elsewhere.test/x"}))
    with pytest.raises(CoreUnavailable):
        await gateway.note_contact(OWNER)
    assert core.paths() == ["/internal/v1/contact"]
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.test:3128")
    client = InternalClient(core_settings(), transport=core.transport())
    try:
        assert client._client.trust_env is False
        assert client._client.follow_redirects is False
    finally:
        await client.aclose()


@pytest.mark.parametrize("status, code", [(401, "AUTH_INVALID"), (403, "FORBIDDEN"), (409, "VERSION_CONFLICT"),
                                          (409, "COMMAND_PAYLOAD_MISMATCH"), (422, "VALIDATION_FAILED")])
async def test_a_refusal_of_the_core_keeps_its_code_and_status(core, gateway, status, code):
    core.answer("/internal/v1/admission/decide", envelope(status, code))
    with pytest.raises(CoreError) as raised:
        await gateway.decide(OWNER, command_id=uuid4(), request_id=uuid4(), expected_version=1, approve=True,
                             reason=None)
    assert type(raised.value) is CoreError
    assert (raised.value.status, raised.value.code) == (status, code)


@pytest.mark.parametrize("answer", [
    envelope(503, "WRITE_UNAVAILABLE"),
    httpx.Response(500, text="Internal Server Error", headers={"content-type": "text/plain"}),
    httpx.Response(502, text="Bad Gateway"),
    httpx.Response(401, text="no envelope"),
    httpx.Response(403, json={"detail": "not the core"}),
    httpx.Response(409, json={"error": {"code": "free text with data"}}),
    httpx.Response(409, json={"error": {"code": 7}}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"command": {}}),
    httpx.Response(204),
    httpx.Response(301, headers={"location": CORE_URL + "/internal/v1/admission/decide/"}),
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut"),
], ids=["503", "500-text", "502", "401-bare", "403-foreign", "409-text-code", "409-number-code", "200-text",
        "200-short", "204", "301", "connect", "read-timeout", "protocol"])
async def test_no_usable_answer_is_core_unavailable_and_the_command_is_not_repeated(core, gateway, answer):
    core.answer("/internal/v1/admission/decide", answer)
    command = uuid4()
    with pytest.raises(CoreUnavailable) as raised:
        await gateway.decide(OWNER, command_id=command, request_id=uuid4(), expected_version=3, approve=False,
                             reason="r")
    assert (raised.value.status, raised.value.code) == (503, "WRITE_UNAVAILABLE")
    # One request, never a second one, and never with another command_id (ВП-6).
    assert [body["command_id"] for body in core.bodies("/internal/v1/admission/decide")] == [str(command)]


async def test_one_call_ends_by_its_deadline_however_slowly_the_core_answers(core):
    async def trickle(request):
        await asyncio.sleep(5)
        return httpx.Response(204)

    core.answer("/internal/v1/contact", trickle)
    settings = core_settings(connect_timeout=timedelta(milliseconds=50), read_timeout=timedelta(milliseconds=100))
    gateway = build_gateway(settings, transport=core.transport())
    started = time.monotonic()
    with pytest.raises(CoreUnavailable):
        await asyncio.wait_for(gateway.note_contact(OWNER), 3)
    assert time.monotonic() - started < 1
    await gateway.aclose()


# -- the lease routes --------------------------------------------------------------------

async def test_the_lease_keeps_holder_and_generation_and_a_held_lease_is_a_standby(core, gateway):
    store = gateway.leases
    first = await store.acquire("bot", holder="bot-1", duration=L)
    assert first == HeldLease("bot-1", "1", "ignored")
    assert await store.acquire("bot", holder="bot-2", duration=L) is None
    assert await store.renew(first, duration=L) == first
    assert core.bodies("/internal/v1/leases/bot/acquire")[0] == {"holder": "bot-1", "duration_seconds": 30}
    assert core.bodies("/internal/v1/leases/bot/renew") == [{"holder": "bot-1", "generation": "1",
                                                           "duration_seconds": 30}]
    assert await store.release(first) is True
    second = await store.acquire("bot", holder="bot-2", duration=L)
    assert second.generation == "2"
    # The former holder can neither renew nor release the new generation.
    with pytest.raises(CoreError) as raised:
        await store.renew(first, duration=L)
    assert (raised.value.status, raised.value.code) == (409, "LEASE_LOST")
    assert await store.release(first) is False
    assert core.bodies("/internal/v1/leases/bot/release")[-1] == {"holder": "bot-1", "generation": "1"}


@pytest.mark.parametrize("answer", [
    envelope(503, "WRITE_UNAVAILABLE"), httpx.Response(500, text="boom"), httpx.ReadTimeout("slow"),
    httpx.Response(200, json={"role": "bot", "holder": "bot-1", "generation": "2", "expires_at": "x"}),
    httpx.Response(200, json={"role": "bot", "holder": "bot-9", "generation": "1", "expires_at": "x"}),
    httpx.Response(200, json={"role": "bot", "holder": "bot-1", "generation": 1, "expires_at": "x"}),
], ids=["503", "500", "timeout", "other-generation", "other-holder", "number-generation"])
async def test_any_failed_renewal_raises(core, gateway, answer):
    lease = await gateway.leases.acquire("bot", holder="bot-1", duration=L)
    core.answer("/internal/v1/leases/bot/renew", answer)
    with pytest.raises(CoreError):
        await gateway.leases.renew(lease, duration=L)


@pytest.mark.parametrize("duration", [timedelta(0), timedelta(seconds=-1), timedelta(milliseconds=1500), 30])
async def test_a_lease_duration_is_whole_seconds(gateway, core, duration):
    with pytest.raises(ValueError):
        await gateway.leases.acquire("bot", holder="bot-1", duration=duration)
    assert core.requests == []


async def test_only_the_bot_lease_is_asked_for(gateway, core):
    with pytest.raises(ValueError):
        await gateway.leases.acquire("delivery", holder="bot-1", duration=L)
    assert core.requests == []


async def test_a_renewal_has_its_own_deadline_when_the_core_hangs(core):
    hang = asyncio.Event()

    async def hanging(request):
        await hang.wait()
        return httpx.Response(200)

    settings = core_settings(connect_timeout=timedelta(milliseconds=50), read_timeout=timedelta(milliseconds=100))
    store = HttpLeaseStore(InternalClient(settings, transport=core.transport()))
    lease = await store.acquire("bot", holder="bot-1", duration=L)
    core.answer("/internal/v1/leases/bot/renew", hanging)
    started = time.monotonic()
    with pytest.raises(CoreUnavailable):
        await asyncio.wait_for(store.renew(lease, duration=L), 3)
    assert time.monotonic() - started < 1


# -- integration: the core's own internal application through ASGITransport ---------------

@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "service_secret"
    # The core accepts only 64 lowercase hex characters (C-KEY-FORMAT); synthetic value.
    path.write_text("0123456789abcdef" * 4 + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def real_core(secret_file):
    """create_internal_app of the core on a disposable schema of wortly_test."""
    pytest.importorskip("fastapi")
    psycopg = pytest.importorskip("psycopg")
    from contextlib import contextmanager
    from datetime import datetime, timezone

    from psycopg import sql

    from wortly.api.internal import InternalConfiguration, create_internal_app
    from wortly.identity.service import IdentityService
    from wortly.jobs.leases import LeaseStore
    from wortly.storage import apply_migrations

    import pg_stand

    parameters = pg_stand.connect_params()
    schema = f"wortly_telegram_test_{uuid4().hex}"
    with psycopg.connect(**parameters, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            @contextmanager
            def connections():
                with psycopg.connect(**parameters, autocommit=True) as connection:
                    connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    yield connection

            with connections() as connection:
                apply_migrations(connection)
                connection.execute("INSERT INTO access_policy(mode) VALUES ('manual') "
                                   "ON CONFLICT (singleton) DO UPDATE SET mode='manual'")
            now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
            # The 1-hour lifetime only lets the test log people in; the bot never does.
            identities = IdentityService(connections, clock=lambda: now, session_lifetime=timedelta(hours=1))
            app = create_internal_app(identity=identities, leases=LeaseStore(connections),
                                      config=InternalConfiguration(max_request_bytes=16384,
                                                                   service_secret=secret_file.read_text().strip()))
            yield app, identities, connections, now
        finally:
            assert schema.startswith("wortly_telegram_test_")
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def person(identities, connections, now, external: str, *, owner: bool = False):
    """A profile linked to ``external`` (logged in by the test, as the Mini App would)."""
    from wortly.identity.telegram import TelegramIdentity

    profile = identities.login(TelegramIdentity(external, "uk-UA", now), browser_language="ru")["profile"]
    with connections() as connection:
        if owner:
            connection.execute("UPDATE profiles SET status='active', role='owner', current_access_request_id=NULL "
                               "WHERE id=%s", (profile["profile_id"],))
            connection.execute("DELETE FROM access_requests WHERE profile_id=%s", (profile["profile_id"],))
            return None
        return connection.execute("SELECT id FROM access_requests WHERE profile_id=%s AND status='pending'",
                                  (profile["profile_id"],)).fetchone()[0]


def bot_core_settings(tmp_path, secret_file):
    """[core] of a bot file naming the core's own secret file, read by the bot's loader."""
    from wortly_telegram.config import load_bot_settings

    token = tmp_path / "token"
    token.write_text("123:fake\n")
    token.chmod(0o600)
    path = tmp_path / "bot.toml"
    path.write_text(f"""
[bot]
instance = "bot-1"
leader_lease_seconds = 60
stop_margin_seconds = 15
standby_retry_seconds = 10
poll_timeout_seconds = 25
[core]
internal_url = "{CORE_URL}"
service_secret_file = "{secret_file}"
connect_timeout_seconds = 2
read_timeout_seconds = 5
[telegram]
bot_token_file = "{token}"
web_app_url = "https://app.example.test/"
connect_timeout_seconds = 1
read_timeout_seconds = 1
write_timeout_seconds = 1
pool_timeout_seconds = 1
""")
    return load_bot_settings(path).core


async def test_the_bot_runs_its_operations_against_the_core_internal_application(real_core, secret_file, tmp_path):
    from wortly.api.internal import PREFIX as CORE_PREFIX
    from wortly.api.internal import SERVICE_SECRET_HEADER as CORE_HEADER

    app, identities, connections, now = real_core
    assert (CORE_HEADER, CORE_PREFIX) == (SERVICE_SECRET_HEADER, "/internal/v1")
    request_id = person(identities, connections, now, "2002")
    person(identities, connections, now, "1001", owner=True)
    gateway = build_gateway(bot_core_settings(tmp_path, secret_file), transport=httpx.ASGITransport(app=app))
    try:
        assert await gateway.profile_summary(OWNER) == {"role": "owner", "status": "admitted", "ui_language": "uk"}
        assert await gateway.profile_summary(TelegramPrincipal("2002")) == {
            "role": "user", "status": "pending", "ui_language": "uk"}
        assert await gateway.profile_summary(TelegramPrincipal("3003")) is None
        await gateway.note_contact(TelegramPrincipal("3003"))

        command = uuid4()
        decided = await gateway.decide(OWNER, command_id=command, request_id=request_id, expected_version=1,
                                       approve=True, reason=None)
        assert decided["command"]["status"] == "applied" and decided["command"]["id"] == str(command)
        assert decided["current"]["access"]["status"] == "admitted"
        # The same update reaches the core again: the same command, already applied.
        again = await gateway.decide(OWNER, command_id=command, request_id=request_id, expected_version=1,
                                     approve=True, reason=None)
        assert again["command"]["status"] == "already_applied"
        with pytest.raises(CoreError) as raised:
            await gateway.decide(TelegramPrincipal("2002"), command_id=uuid4(), request_id=request_id,
                                 expected_version=1, approve=False, reason=None)
        assert (raised.value.status, raised.value.code) == (403, "FORBIDDEN")
        mode = await gateway.change_mode(OWNER, command_id=uuid4(), expected_version=1, mode="automatic")
        assert mode["current"]["mode"] == "automatic"
        with pytest.raises(CoreError) as raised:
            await gateway.change_mode(OWNER, command_id=uuid4(), expected_version=1, mode="manual")
        assert (raised.value.status, raised.value.code) == (409, "VERSION_CONFLICT")

        store = gateway.leases
        lease = await store.acquire("bot", holder="bot-1", duration=L)
        assert lease.holder == "bot-1" and lease.generation.isdigit()
        assert await store.acquire("bot", holder="bot-2", duration=L) is None
        assert await store.renew(lease, duration=L) == lease
        assert await store.release(lease) is True
        with pytest.raises(CoreError) as raised:
            await store.renew(lease, duration=L)
        assert (raised.value.status, raised.value.code) == (409, "LEASE_LOST")
        successor = await store.acquire("bot", holder="bot-2", duration=L)
        assert int(successor.generation) == int(lease.generation) + 1
    finally:
        await gateway.aclose()


async def test_a_wrong_secret_is_refused_by_the_core_and_changes_nothing(real_core):
    app, identities, connections, now = real_core
    gateway = build_gateway(core_settings(service_secret="another-secret"), transport=httpx.ASGITransport(app=app))
    try:
        with pytest.raises(CoreError) as raised:
            await gateway.leases.acquire("bot", holder="bot-1", duration=L)
        assert (raised.value.status, raised.value.code) == (401, "AUTH_INVALID")
        with connections() as connection:
            assert connection.execute("SELECT count(*) FROM process_leases").fetchone()[0] == 0
    finally:
        await gateway.aclose()


def test_the_core_schema_still_names_what_the_client_sends():
    # The offline internal schema of the core (docs/openapi_internal.json) has every route
    # the client posts to, and each request body the client builds has exactly its fields.
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parents[2] / "core" / "docs" / "openapi_internal.json")
                        .read_text(encoding="utf-8"))
    paths = schema["paths"]
    assert set(ROUTES) <= {path.replace("{role}", "bot") for path in paths}
    components = schema["components"]["schemas"]
    fields = {name: set(model.get("properties", {})) for name, model in components.items()}
    assert fields["DecideRequest"] == {"actor_telegram_id", "command_id", "request_id", "expected_version",
                                       "approve", "reason"}
    assert fields["ModeRequest"] == {"actor_telegram_id", "command_id", "expected_version", "mode"}
    assert fields["LeaseAcquireRequest"] == {"holder", "duration_seconds"}
    assert fields["LeaseRenewRequest"] == {"holder", "generation", "duration_seconds"}
    assert fields["LeaseReleaseRequest"] == {"holder", "generation"}
    assert fields["ActorRequest"] == {"actor_telegram_id"}
