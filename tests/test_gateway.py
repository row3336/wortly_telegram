"""The core gateway, variant A of fork Р1: core calls in a worker thread, the subject from the update only."""

from __future__ import annotations

import threading
from uuid import uuid4

import pytest
from telegram import Update

from fakes.telegram_api import callback_update, message_update, user
from wortly.identity.principal import TelegramPrincipal
from wortly_telegram.gateway import CoreGateway, principal_of


class Admission:
    """Stands in for wortly.identity.admission_adapter.AdmissionAdapter."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, TelegramPrincipal, dict, str]] = []

    def _record(self, name, principal, arguments):
        self.calls.append((name, principal, arguments, threading.current_thread().name))
        return {"operation": name}

    def profile_summary(self, principal):
        return self._record("profile_summary", principal, {})

    def decide_as(self, principal, **arguments):
        return self._record("decide_as", principal, arguments)

    def change_mode_as(self, principal, **arguments):
        return self._record("change_mode_as", principal, arguments)


async def test_core_calls_run_outside_the_event_loop_thread_and_pass_every_argument():
    admission, principal = Admission(), TelegramPrincipal("5005")
    gateway = CoreGateway(admission)
    command, request = uuid4(), uuid4()
    assert await gateway.profile_summary(principal) == {"operation": "profile_summary"}
    await gateway.decide(principal, command_id=command, request_id=request, expected_version=3, approve=False,
                         reason="short")
    await gateway.change_mode(principal, command_id=command, expected_version=7, mode="automatic")
    names = [call[0] for call in admission.calls]
    assert names == ["profile_summary", "decide_as", "change_mode_as"]
    assert all(call[1] is principal for call in admission.calls)
    assert admission.calls[1][2] == dict(command_id=command, request_id=request, expected_version=3,
                                         approve=False, reason="short")
    assert admission.calls[2][2] == dict(command_id=command, expected_version=7, mode="automatic")
    # Each synchronous core call ran in a worker thread, never on the loop's thread.
    assert all(call[3] != threading.current_thread().name for call in admission.calls)


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


def test_build_gateway_wires_the_core_admission_adapter():
    pytest.importorskip("psycopg")
    from datetime import datetime, timedelta, timezone

    from wortly.identity.admission_adapter import AdmissionAdapter
    from wortly_telegram.gateway import build_gateway

    gateway = build_gateway(lambda: None, clock=lambda: datetime.now(timezone.utc),
                            session_lifetime=timedelta(hours=1))
    assert isinstance(gateway._admission, AdmissionAdapter)
