"""The delivery process's parts that need no database: the thread bridge, the gate, the start check."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from wortly.jobs.kinds import discover as discover_kinds
from wortly_telegram.delivery.main import DeliveryGate, StartError, checked_kinds, loop_runner
from wortly_telegram.messages.registry import TemplateRegistry, discover


def test_the_bridge_runs_the_coroutine_on_the_bot_loop_and_returns_its_result(bot_loop):
    run = loop_runner(bot_loop)

    async def where() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    assert run(where()) is bot_loop


def test_the_bridge_raises_the_coroutines_exception(bot_loop):
    async def fail():
        raise LookupError("x")

    with pytest.raises(LookupError):
        loop_runner(bot_loop)(fail())


class Capacity:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def wait_until_open(self) -> None:
        self.calls.append("wait_until_open")

    def wait_for_slot(self) -> None:
        self.calls.append("wait_for_slot")


class Channel:
    def __init__(self, rejected: bool) -> None:
        self.token_rejected = rejected


def test_the_gate_passes_to_the_capacity_while_the_token_works():
    capacity = Capacity()
    gate = DeliveryGate(capacity, Channel(False), threading.Event())
    gate.wait_until_open()
    gate.wait_for_slot()
    assert capacity.calls == ["wait_until_open", "wait_for_slot"]


def test_a_rejected_token_closes_the_gate_until_the_process_stops():
    capacity, stop = Capacity(), threading.Event()
    gate = DeliveryGate(capacity, Channel(True), stop)
    waiter = threading.Thread(target=gate.wait_until_open)
    waiter.start()
    time.sleep(0.2)
    assert waiter.is_alive(), "no job may be claimed while the token is rejected"
    stop.set()
    waiter.join(5)
    assert not waiter.is_alive()
    assert capacity.calls == []


def test_the_start_check_accepts_the_shipped_kinds_and_templates():
    checked_kinds(discover_kinds().names, discover())


def test_the_start_check_refuses_a_kind_without_a_template():
    with pytest.raises(StartError, match="access_approved"):
        checked_kinds(frozenset({"access_approved"}), TemplateRegistry([]))


def test_the_start_check_refuses_a_kind_without_a_retry_rule():
    template = discover().template("access_approved")
    renamed = type(template)("brand_new_kind", template.render, template.examples)
    with pytest.raises(StartError, match="retry rule"):
        checked_kinds(frozenset({"brand_new_kind"}), TemplateRegistry([renamed]))
