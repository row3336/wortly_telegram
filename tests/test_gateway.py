"""The core gateway over the internal API (answer 2.4): each method's body, answer and subject.

The core is FakeCore behind httpx.MockTransport (fakes/core_gateway.py); the HTTP rules
themselves (secret, deadline, refusals) are tested in test_internal_client.py.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from telegram import Update

from fakes.core_gateway import FakeCore, core_settings, forbid_network
from fakes.telegram_api import callback_update, message_update, user
from wortly.identity.principal import TelegramPrincipal
from wortly_telegram.gateway import CoreUnavailable, HttpCoreGateway, HttpLeaseStore, build_gateway, principal_of


PRINCIPAL = TelegramPrincipal("5005")


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


async def test_each_method_posts_the_actor_and_its_arguments(core, gateway):
    command, request = uuid4(), uuid4()
    assert await gateway.profile_summary(PRINCIPAL) is None
    await gateway.note_contact(PRINCIPAL)
    decided = await gateway.decide(PRINCIPAL, command_id=command, request_id=request, expected_version=3,
                                   approve=False, reason="short")
    assert decided["command"]["id"] == str(command)
    await gateway.decide(PRINCIPAL, command_id=command, request_id=request, expected_version=3, approve=True,
                         reason=None)
    await gateway.change_mode(PRINCIPAL, command_id=command, expected_version=7, mode="automatic")
    assert core.bodies("/internal/v1/admission/summary") == [{"actor_telegram_id": "5005"}]
    assert core.bodies("/internal/v1/contact") == [{"actor_telegram_id": "5005"}]
    assert core.bodies("/internal/v1/admission/decide") == [
        {"actor_telegram_id": "5005", "command_id": str(command), "request_id": str(request),
         "expected_version": "3", "approve": False, "reason": "short"},
        # Without a reason the field is left out, as the core's schema makes it optional.
        {"actor_telegram_id": "5005", "command_id": str(command), "request_id": str(request),
         "expected_version": "3", "approve": True}]
    assert core.bodies("/internal/v1/admission/change_mode") == [
        {"actor_telegram_id": "5005", "command_id": str(command), "expected_version": "7", "mode": "automatic"}]


async def test_a_retried_contact_carries_its_age(core, gateway):
    # PP31 finding 1.8: a live contact sends no age, a retry tells how long ago it happened.
    await gateway.note_contact(PRINCIPAL, age_ms=0)
    await gateway.note_contact(PRINCIPAL, age_ms=12_345)
    assert core.bodies("/internal/v1/contact") == [{"actor_telegram_id": "5005"},
                                                   {"actor_telegram_id": "5005", "age_ms": 12_345}]


@pytest.mark.parametrize("age", [-1, 1.5, "5", True])
async def test_a_malformed_contact_age_is_refused_before_the_core_is_asked(core, gateway, age):
    with pytest.raises(ValueError):
        await gateway.note_contact(PRINCIPAL, age_ms=age)
    assert core.bodies("/internal/v1/contact") == []


async def test_the_profile_summary_is_the_core_answer(core, gateway):
    profile = {"role": "owner", "status": "admitted", "ui_language": None}
    core.answer("/internal/v1/admission/summary", httpx.Response(200, json={"profile": profile}))
    assert await gateway.profile_summary(PRINCIPAL) == profile


@pytest.mark.parametrize("answer", [
    {}, {"profile": None, "extra": 1}, {"profile": {"role": "admin", "status": "admitted", "ui_language": "uk"}},
    {"profile": {"role": "owner", "status": "active", "ui_language": "uk"}},
    {"profile": {"role": "owner", "status": "admitted", "ui_language": "de"}},
    {"profile": {"role": "owner", "status": "admitted"}}, {"profile": "owner"}, [],
])
async def test_a_summary_outside_the_contract_is_core_unavailable(core, gateway, answer):
    core.answer("/internal/v1/admission/summary", httpx.Response(200, json=answer))
    with pytest.raises(CoreUnavailable):
        await gateway.profile_summary(PRINCIPAL)


@pytest.mark.parametrize("call", [
    lambda gateway: gateway.profile_summary("5005"),
    lambda gateway: gateway.note_contact(5005),
    lambda gateway: gateway.decide(PRINCIPAL, command_id=uuid4(), request_id=uuid4(), expected_version="3",
                                   approve=True, reason=None),
    lambda gateway: gateway.decide(PRINCIPAL, command_id=uuid4(), request_id=uuid4(), expected_version=0,
                                   approve=True, reason=None),
    lambda gateway: gateway.decide(PRINCIPAL, command_id=uuid4(), request_id=uuid4(), expected_version=True,
                                   approve=True, reason=None),
    lambda gateway: gateway.decide(PRINCIPAL, command_id=uuid4(), request_id=uuid4(), expected_version=1,
                                   approve="yes", reason=None),
    lambda gateway: gateway.decide(PRINCIPAL, command_id="not-a-uuid", request_id=uuid4(), expected_version=1,
                                   approve=True, reason=None),
], ids=["principal-text", "principal-number", "version-text", "version-zero", "version-bool", "approve-text",
        "command-id"])
async def test_a_malformed_call_is_refused_before_the_core_is_asked(core, gateway, call):
    with pytest.raises((TypeError, ValueError)):
        await call(gateway)
    assert core.requests == []


def test_the_gateway_and_its_lease_store_share_one_client(core):
    gateway = build_gateway(core_settings(), transport=core.transport())
    assert isinstance(gateway, HttpCoreGateway) and isinstance(gateway.leases, HttpLeaseStore)
    assert gateway.leases._client is gateway.client


def test_the_principal_is_the_sender_of_the_update():
    press = Update.de_json(callback_update("acc:a:777:1", from_user=user(5005)), None)
    assert principal_of(press).external_id == "5005"
    text = Update.de_json(message_update("777", from_user=user(6006)), None)
    assert principal_of(text).external_id == "6006"


def test_an_update_without_a_sender_has_no_principal():
    channel_post = Update.de_json({"update_id": 1, "channel_post": {
        "message_id": 1, "date": 0, "text": "x", "chat": {"id": -1001, "type": "channel", "title": "c"}}}, None)
    with pytest.raises(ValueError):
        principal_of(channel_post)
