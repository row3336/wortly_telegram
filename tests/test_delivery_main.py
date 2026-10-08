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
from wortly.jobs.loop import UnservedReport
from wortly_telegram.delivery.main import (
    DeliveryGate, StartError, _deliver, checked_kinds, loop_runner, reported_unserved, serve,
)
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


class FakeRepository:
    """Stands for JobRepository: answers which unfinished jobs have a kind outside the given ones."""

    def __init__(self, waiting: frozenset[str] = frozenset()) -> None:
        self.waiting = waiting
        self.asked: list[frozenset[str]] = []

    def unserved_kinds(self, kinds) -> frozenset[str]:
        self.asked.append(frozenset(kinds))
        return self.waiting - frozenset(kinds)


def test_nothing_is_logged_without_jobs_of_unknown_kinds(caplog):
    kinds = discover_kinds().names
    repository = FakeRepository(waiting=frozenset(kinds))
    assert reported_unserved(kinds, repository, UnservedReport()) == frozenset()
    assert repository.asked == [kinds]
    assert caplog.records == []


def test_jobs_of_unknown_kinds_are_logged_once_by_name_and_do_not_refuse_the_start(caplog):
    repository = FakeRepository(waiting=frozenset({"brand_new_kind", "access_approved"}))
    with caplog.at_level("ERROR"):
        unserved = reported_unserved(discover_kinds().names, repository, UnservedReport())
    assert unserved == frozenset({"brand_new_kind"})
    assert [record.getMessage() for record in caplog.records] == [
        "1 job kinds this release cannot deliver are waiting: brand_new_kind"]


def test_serve_starts_the_delivery_with_a_job_of_an_unknown_kind(monkeypatch, caplog):
    from wortly_telegram.delivery import main

    started = []

    async def fake_deliver(settings, *, kinds, templates, repository, unserved_report):
        started.append((kinds.names, repository))
        assert isinstance(unserved_report, UnservedReport)  # the loop gets the start's report
        return 0

    monkeypatch.setattr(main, "_deliver", fake_deliver)
    repository = FakeRepository(waiting=frozenset({"brand_new_kind"}))
    closed = []
    pool = SimpleNamespace(connection=None, close=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, "wortly.jobs.repository",
                        SimpleNamespace(JobRepository=lambda connections: repository))
    monkeypatch.setitem(sys.modules, "wortly.operations.connections", SimpleNamespace(
        lock_timeout_connections=lambda connection, timeout: None, open_connection_pool=lambda *a, **k: pool))
    settings = SimpleNamespace(database=None, lock_timeout=None, telegram=TELEGRAM_SETTINGS)
    with caplog.at_level("ERROR"):
        assert serve(settings) == 0
    assert [names for names, _ in started] == [discover_kinds().names]
    assert started[0][1] is repository  # the same repository: the jobs stay untouched
    assert repository.asked == [discover_kinds().names]
    assert closed == [True]
    assert [record.getMessage() for record in caplog.records] == [
        "1 job kinds this release cannot deliver are waiting: brand_new_kind"]


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


def test_the_start_line_and_the_loop_share_one_report(caplog):
    from wortly.jobs.loop import UnservedReport

    now = [0.0]
    report = UnservedReport(monotonic=lambda: now[0])
    repository = FakeRepository(waiting=frozenset({"future_a", "future_b"}))
    names = discover_kinds().names
    with caplog.at_level("ERROR"):
        reported_unserved(names, repository, report)
        now[0] = 30 * 60
        report.note(repository.unserved_kinds(names))  # the loop's poll
        assert [r.getMessage() for r in caplog.records] == [
            "2 job kinds this release cannot deliver are waiting: future_a, future_b"]
        now[0] = 61 * 60
        report.note(repository.unserved_kinds(names))
        assert len(caplog.records) == 2
