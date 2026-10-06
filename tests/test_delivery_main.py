"""The delivery process's parts that need no database: the thread bridge, the gate, the start check."""

from __future__ import annotations

import asyncio
import signal
import sys
from types import SimpleNamespace
import threading
import time

import pytest

from conftest import TELEGRAM_SETTINGS
from wortly.jobs.kinds import discover as discover_kinds
from wortly_telegram.delivery.main import DeliveryGate, StartError, _deliver, checked_kinds, loop_runner
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


class SlowToFinish:
    """Stands for the core's DeliveryLoop: runs until stopped, then needs a moment to finish its last send."""

    started = threading.Event()
    finished = threading.Event()

    def __init__(self, repository, **parts) -> None:
        type(self).started.clear()
        type(self).finished.clear()

    def run(self, stop: threading.Event) -> None:
        type(self).started.set()
        stop.wait(10)  # a bounded wait: a failing test must not leave a thread that hangs the run
        time.sleep(0.3)
        type(self).finished.set()


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
async def test_the_delivery_process_waits_for_the_last_send_after_a_signal(monkeypatch, telegram, number):
    from wortly_telegram import app

    real_make_bot = app.make_bot
    monkeypatch.setattr(app, "make_bot", lambda settings: real_make_bot(
        settings, httpx_kwargs={"transport": telegram.transport()}))
    # The core's loop module needs the database driver; the process only takes DeliveryLoop from it.
    monkeypatch.setitem(sys.modules, "wortly.jobs.loop", SimpleNamespace(DeliveryLoop=SlowToFinish))
    handlers = {}
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler",
                        lambda signal_number, callback, *args: handlers.setdefault(signal_number, callback))
    settings = SimpleNamespace(telegram=TELEGRAM_SETTINGS, messages_per_second=10, loop=None)
    task = asyncio.create_task(_deliver(settings, kinds=discover_kinds(), templates=discover(), repository=None))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if SlowToFinish.started.is_set():
            break
    assert SlowToFinish.started.is_set(), "the delivery loop runs in its thread"
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    assert not task.done()
    handlers[number]()
    assert await asyncio.wait_for(task, timeout=10) == 0
    # The process returns only after the loop's last send is over; the bot is shut down after that.
    assert SlowToFinish.finished.is_set()
