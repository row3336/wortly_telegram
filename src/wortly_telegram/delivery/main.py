"""Process entry of the delivery role: ``python -m wortly_telegram.delivery.main --config <file>``.

Design T-D §2.1, §3.5, §3.7 (ВП-13). The core's delivery loop (wortly.jobs.loop) and
psycopg are synchronous, PTB is asynchronous. The main thread runs the asyncio loop
that owns one initialised telegram.Bot; a worker thread runs the delivery loop. The
synchronous channel hands each send to the main loop with
``asyncio.run_coroutine_threadsafe`` and waits for the result; it has no timeout of its
own, the HTTPXRequest timeouts bound it.

Start: the configuration is complete; every job kind of the core has a builder, a
retry rule (checked by DeliveryLoop) and a template here; an unfinished job waiting for a
kind without a builder is only logged (JobRepository.unserved_kinds; owner decisions В2, Q21,
Q22: such jobs stay untouched, the other kinds are delivered, a new set of kinds is logged at
once and the same set at most once an hour, see wortly.jobs.loop.UnservedReport); getMe accepts the token
(design §2.3). Otherwise the process does not start. While running, an InvalidToken
answer closes the gate: no further job is claimed until the process is restarted.
Stopping (SIGTERM, SIGINT) takes no new claim and lets the current attempt finish
(§2.4.4). Database connections reach the writable leader only and each carries a
lock_timeout (QA C-JOBS). Everything runs inside private_logs (logs.py).

The core is imported inside the functions that start the process, so the bridge and
the gate import without a database driver.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Coroutine, Sequence
import logging
from pathlib import Path
import signal
import sys
import threading
from typing import Any, Protocol

from telegram.error import InvalidToken, TelegramError


logger = logging.getLogger(__name__)


def loop_runner(loop: asyncio.AbstractEventLoop) -> Callable[[Coroutine], Any]:
    """Run a coroutine on ``loop`` from another thread and wait for its result."""

    def run(coroutine: Coroutine) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    return run


class _Capacity(Protocol):
    def wait_until_open(self) -> None: ...

    def wait_for_slot(self) -> None: ...


class _Channel(Protocol):
    @property
    def token_rejected(self) -> bool: ...


class DeliveryGate:
    """The capacity the core's loop waits on: the rate gate, closed for good by a rejected token."""

    def __init__(self, capacity: _Capacity, channel: _Channel, stop: threading.Event) -> None:
        self._capacity = capacity
        self._channel = channel
        self._stop = stop

    def wait_until_open(self) -> None:
        if self._channel.token_rejected:
            logger.warning("delivery stopped taking jobs: Telegram rejected the bot token")
            self._stop.wait()
            return
        self._capacity.wait_until_open()

    def wait_for_slot(self) -> None:
        self._capacity.wait_for_slot()


class StartError(RuntimeError):
    """The process cannot start; the message names the reason, never a value."""


def checked_kinds(kinds: frozenset[str], templates) -> None:
    """Every kind the core can claim must be deliverable here (design §2.3, §3.8 п.6)."""
    from wortly.jobs.retry_policy import kind_class

    missing = templates.missing(kinds)
    if missing:
        raise StartError("job kinds without a template: " + ", ".join(sorted(missing)))
    for kind in sorted(kinds):
        try:
            kind_class(kind)
        except ValueError:
            raise StartError(f"job kind {kind} has no retry rule") from None


def reported_unserved(kinds: frozenset[str], repository, report) -> frozenset[str]:
    """Log unfinished jobs of kinds this release cannot build; never refuse the start (Q21, Q22).

    Such jobs stay untouched and the other kinds are delivered. ``report`` (the core's
    UnservedReport) applies the log rule; the same object goes to the delivery loop, so
    the start line and the loop's polls share one state and never double up.
    """
    unserved = repository.unserved_kinds(kinds)
    report.note(unserved)
    return unserved


async def _deliver(settings, *, kinds, templates, repository, unserved_report=None) -> int:
    from wortly.jobs.loop import DeliveryLoop

    from ..app import make_bot
    from .capacity import TelegramCapacity
    from .channel import TelegramChannel

    stop = threading.Event()
    loop = asyncio.get_running_loop()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, stop.set)
    bot = make_bot(settings.telegram)
    try:
        await bot.initialize()
    except InvalidToken:
        logger.error("delivery not started: Telegram rejected the bot token")
        return 2
    except TelegramError as error:
        logger.error("delivery not started: getMe failed: %s", type(error).__name__)
        return 1
    try:
        capacity = TelegramCapacity(settings.messages_per_second, wait=stop.wait)
        channel = TelegramChannel(bot, templates=templates, web_app_url=settings.telegram.web_app_url,
                                  capacity=capacity, run=loop_runner(loop))
        delivery = DeliveryLoop(repository, config=settings.loop, kinds=kinds,
                                capacity=DeliveryGate(capacity, channel, stop), channel=channel,
                                unserved_report=unserved_report)
        worker = threading.Thread(target=delivery.run, args=(stop,), name="wortly-delivery")
        worker.start()
        logger.info("delivery started")
        # The loop must keep running until the worker's last send has finished.
        await asyncio.to_thread(worker.join)
    finally:
        await bot.shutdown()
    return 0


def serve(settings) -> int:
    from wortly.jobs.kinds import discover
    from wortly.jobs.loop import UnservedReport
    from wortly.jobs.repository import JobRepository
    from wortly.operations.connections import lock_timeout_connections, open_connection_pool

    from ..messages.registry import discover as discover_templates

    kinds = discover()
    templates = discover_templates()
    checked_kinds(kinds.names, templates)
    pool = open_connection_pool(settings.database, name="wortly-delivery")
    try:
        repository = JobRepository(lock_timeout_connections(pool.connection, settings.lock_timeout))
        report = UnservedReport()
        reported_unserved(kinds.names, repository, report)
        return asyncio.run(_deliver(settings, kinds=kinds, templates=templates, repository=repository,
                                    unserved_report=report))
    finally:
        pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    from wortly.jobs.kinds import KindRegistryError
    from wortly.operations.config import ConfigurationError

    from ..config import load_delivery_settings
    from ..logs import log_secrets, private_logs
    from ..messages.registry import TemplateRegistryError

    parser = argparse.ArgumentParser(
        prog="python -m wortly_telegram.delivery.main",
        description="Deliver Wortly jobs through Telegram from an explicit configuration file.")
    parser.add_argument("--config", required=True, type=Path, help="TOML file; every key is required")
    arguments = parser.parse_args(argv)
    try:
        settings = load_delivery_settings(arguments.config)
    except ConfigurationError as error:
        print(f"wortly delivery: configuration error: {error}", file=sys.stderr)
        return 2
    with private_logs(log_secrets(settings.telegram.bot_token, settings.database)):
        try:
            return serve(settings)
        except (KindRegistryError, TemplateRegistryError, StartError) as error:
            # Class and the registry's own message: kind and module names, never data.
            logger.error("delivery not started: %s: %s", type(error).__name__, error)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
