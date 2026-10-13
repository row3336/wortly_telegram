"""The bot's and the delivery's side of the user-path trail (R3-03; events TZ §3, §4.3, §4.4).

The writer, its closed lists and its field checks live in the core (wortly.operations.trail);
this module only gives the adapter's two processes what the writer needs and keeps the
adapter's own rules in one place:

- ``trace``: a random UUIDv4 that the bot makes for each update it handles
  (``begin_update``). Every call of the core made while that update is handled carries it in
  the ``X-Wortly-Trace-Id`` header and in its ``b.core`` line, so ``b.update`` ->
  ``b.core`` -> the core's own lines -> ``b.reply`` are one chain (events TZ §2.3 п.4, V5).
  A call outside an update (the poller's lease, a background retry of a contact) gets a
  fresh trace of its own (``call_trace``). The gateway repeats no call, so a trace is
  never a repeat of another attempt.
- ``emit``: writes one line; it never raises and never takes a value from the caller that
  the writer does not check. What this adapter must never write is listed in
  ``FORBIDDEN_FIELDS``: the writer already refuses any field outside its table, and the
  list makes the rule explicit and testable here.
- The bot's own command ID (``uuid5`` of chat and message, router.command_id), the chat,
  the Telegram user, names, texts, ``callback_data``, ``logical_key`` and ``snapshot`` are
  derived from, or hold, personal data; no function of this module accepts them.

``fn`` of ``b.update`` is a closed list: the router registers the names of its service functions
(and "default") with ``allow_functions`` when it installs them; any other name is dropped.

Outside ``process_trail`` the writer has no handler, so the adapter's tests and tools that
do not configure it write nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import metadata
import re
from typing import Any
from uuid import uuid4

from wortly.operations.trail import allow_functions, configured, trail


TRACE_HEADER = "X-Wortly-Trace-Id"
BOT = "bot"
DELIVERY = "delivery"

# Fields that must never appear in a line of this adapter (events TZ §4.4, §5, root decisions
# of 13.10). Not an allow-list (the writer's table is that): a guard for ``emit`` and a name list
# for the negative tests.
FORBIDDEN_FIELDS = frozenset({
    "cmd", "command_id", "chat_id", "telegram_user_id", "user_id", "username", "first_name", "name",
    "text", "callback_data", "logical_key", "snapshot", "facts", "payload", "prof", "inst", "doc",
    "sess", "grade", "areq",
})

_update_trace: ContextVar[str | None] = ContextVar("wortly_telegram_update_trace", default=None)
_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,32}")


__all__ = ["allow_functions", "begin_update", "build_version", "call_trace", "emit", "end_update",
           "process_trail", "update_trace", "BOT", "DELIVERY", "TRACE_HEADER", "FORBIDDEN_FIELDS"]


def begin_update() -> str:
    """Start the trace of one update: a random UUIDv4, kept for the handlers of this update."""
    trace = str(uuid4())
    _update_trace.set(trace)
    return trace


def end_update() -> None:
    """Forget the update's trace (a background task of the update must not inherit it)."""
    _update_trace.set(None)


def update_trace() -> str | None:
    """The trace of the update being handled, or None outside an update."""
    return _update_trace.get()


def call_trace() -> str:
    """The trace of one call of the core: the update's, or a fresh one outside an update."""
    return _update_trace.get() or str(uuid4())


def emit(event: str, /, *, component: str, **fields: Any) -> None:
    """Write one trail line of ``component``; never raises, never writes a forbidden field."""
    try:
        # A field given as None is absent; a forbidden one is never passed on.
        given = {name: value for name, value in fields.items()
                 if value is not None and name not in FORBIDDEN_FIELDS}
        trail(event, comp=component, **given)
    except Exception:  # noqa: BLE001 - a trail line must never break the work it describes
        return


def build_version() -> str:
    """The version of the installed adapter build, or "unknown" when the code runs from a source tree."""
    try:
        version = metadata.version("wortly-telegram")
    except Exception:  # noqa: BLE001 - metadata is absent in a source tree
        return "unknown"
    return version if _VERSION.fullmatch(version) else "unknown"


@contextmanager
def process_trail(component: str) -> Iterator[None]:
    """The trail's own INFO handler on the process's stderr for the process entry's block."""
    with configured(component=component, version=build_version()):
        yield
