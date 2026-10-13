"""Which code answers an update: service functions, /start and the default handler (design T-D §7.1, §6).

Service functions are found automatically (ВП-21): every module of
wortly_telegram.service declares ``SERVICE_FUNCTIONS`` (the owner's admission of T2 is
wortly_telegram.service.access). A package adds a module and edits no shared list. A service module without the
declaration, a command or callback prefix claimed twice stops the start.

- Only private chats are served: Telegram accepts a web_app button only there.
- Only new messages and button presses are handled; an edited message is neither
  requested nor handled (ВП-6), because it would repeat the command of its original.
  The bot's own membership update (my_chat_member) is requested for one purpose only:
  the contact note of an unblock (below). It is no command and gets no answer.
- ``audience`` filters the interface only. The core checks every right again for the
  principal under its locks (§7.1 п.2); the role in a summary is not a right, so an
  owner function also needs an admitted status (QA C-ADAPTER).
- callback_data is ``<prefix>:<action>:<object references>`` and never names the
  actor (§7.1 п.3); the actor is always ``update.effective_user`` (gateway.principal_of).
- ``command_id`` is derived from the update itself (ВП-6), so the same update
  delivered twice (two pollers, a restart) is the same command for the core.

Contact (answer 2.5): any private message or button press of a user proves the bot is not
blocked (2.28б), and so does Telegram's report that the user unblocked the bot: a
my_chat_member update of a private chat, made by that chat's user, whose new status is
"member" (Я18). The first handler of every such update (handler group -1, before the
service functions and the default answer) calls ``gateway.note_contact`` exactly once; the
core lifts the "bot blocked" mark and releases the one-off notices it kept back (2.28а).
A block reported the same way ("kicked") calls nothing: the mark is set only by a
delivery that Telegram refused as blocked (answer 2.5). The call is best effort: a
failure or a timeout (CONTACT_TIMEOUT) is logged by class only and never stops the answer
to the user. Whether a real Telegram bot delivers the unblock update is checked on a test
bot before acceptance (Я18, TC §13.6); the tests here hand the update to the Application.

A contact the core did not take because it was unreachable, too slow or failing
(CoreUnavailable, a timeout) would otherwise be lost: the update is confirmed to Telegram
anyway, and an unblock update never comes again (PP31, finding 1.8). It is tried again in
the background after each pause of CONTACT_RETRY_DELAYS, then given up; one retry per
user at a time, since one successful contact covers every earlier one; a newer unanswered
contact moves that retry's time to itself, and one that came while a retry's call ran is
noted right after that call succeeds. A retry tells the core how long ago the contact
happened, so the core dates it back and does not lift a
block the user made after it. The answer to the user and the next updates never wait
for it. A refusal of the core (another CoreError)
is not repeated. The retries live in this process only: a restart drops them. The update
is not held back unconfirmed instead, because one failing contact would then stop every
later update of the bot.

Text input (T2, answer 2.6 б): a service function may claim the next plain text of a
chat with ``text_input`` (the owner's reason for a rejection). The router hands a plain
text, not a command, to the first function whose input waits for that chat and whose
audience still admits the sender; otherwise the default answer follows. Every command
and every button press of a private chat first cancels each waiting input (handler group
-2), before any handler runs: (Q55, approved by R12-14) a command or a button ends the
input. A function's own button then starts a new one in its handler.

Trail (R3-03, events TZ §4.4): every update that reaches a handler of group 0 writes one
``b.update`` line once handled: its trace, ``kind`` (message or callback), ``fn`` (the name of the
service function, or "default" for the answer of rule ВП-27), the outcome (handled, denied when
the audience did not admit the sender, ignored for a chat that is not private; failed, with the
exception class, when the handler raised) and the milliseconds. The trace is made by the
first handler of every update (group -2) and carried by every call of the core made while the
update is handled (trail.py). The line never holds the chat, the sender, the text, the button's
data or ``command_id`` below: that ID is derived from the chat and the message, so it would lead
back to a person without a database and is written nowhere.

Default handler — rule ВП-27, made the owner's decision by answer 2.7 (06.10.2026; Q23): any other text,
an old command (/undo) or an unknown button gets the /start answer, a short text with
the Mini App button (the old bot's handler of orphaned text, R-TG 1.03, led to /start
too). Only ``answer_anything_else`` in this module holds that rule.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from importlib import import_module
import logging
import pkgutil
import re
import time
from typing import Any, Literal, Protocol
from uuid import UUID, uuid5

from telegram import Bot, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters,
)

from .gateway import CoreUnavailable, principal_of
from .reply import send_reply
from .start import send_start
from .trail import BOT, allow_functions, begin_update, emit, end_update, update_trace


logger = logging.getLogger(__name__)

SERVICE_PACKAGE = "wortly_telegram.service"
# What the poller asks Telegram for: exactly the update types handled below (ВП-6).
ALLOWED_UPDATES = ("message", "callback_query", "my_chat_member")

# PTB handles updates one at a time, so a hanging core call would hold the whole bot;
# the contact note is best effort and is given up after this many seconds.
CONTACT_TIMEOUT = 2.0
# Pauses before each background retry of a contact the core did not take: about four
# minutes in all, enough for a core restart, then the retry ends (root decision, pending
# owner: technical values, PP31 finding 1.8).
CONTACT_RETRY_DELAYS = (2.0, 10.0, 30.0, 60.0, 120.0)
# The core's bound of a contact's age (internal API ContactRequest.age_ms).
CONTACT_AGE_MAX_MS = 600_000

# Fixed for good: the same update must give the same command_id in every release.
COMMAND_NAMESPACE = UUID("34f1fd3d-1a7a-4b83-983e-b6478ea6bbc1")

Audience = Literal["anyone", "admitted", "owner"]
_COMMAND = re.compile(r"[a-z0-9_]{1,32}")
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,15}")


class Gateway(Protocol):
    async def note_contact(self, principal: Any, *, age_ms: int = 0) -> None: ...

    async def profile_summary(self, principal: Any) -> dict | None: ...


@dataclass(frozen=True)
class BotContext:
    """What every handler of the bot gets besides the update."""

    bot: Bot
    gateway: Gateway
    web_app_url: str
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    # Background contact retries by Telegram ID; the task objects are kept so they run on.
    contact_retries: dict[str, asyncio.Task] = field(default_factory=dict, repr=False, compare=False)
    # The newest unanswered contact of each user with a retry: its age is counted from it.
    contact_seen: dict[str, float] = field(default_factory=dict, repr=False, compare=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)


Handle = Callable[[Update, BotContext], Awaitable[None]]


@dataclass(frozen=True)
class TextInput:
    """A function's claim on the next plain text of a chat (design T-D §5.2 Р5).

    ``waiting(update)`` tells, without the core, whether this sender's chat waits for
    text; ``handle`` takes that text; ``cancel(update)`` drops the chat's input. The state
    lives in the function's own module, in the bot process's memory.
    """

    waiting: Callable[[Update], bool]
    handle: Handle
    cancel: Callable[[Update], None]

    def __post_init__(self) -> None:
        if not (callable(self.waiting) and callable(self.handle) and callable(self.cancel)):
            raise ValueError("A text input needs waiting, handle and cancel callables")


@dataclass(frozen=True)
class ServiceFunction:
    name: str
    commands: tuple[str, ...]
    callback_prefix: str | None
    audience: Audience
    handle: Handle
    text_input: TextInput | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("A service function needs a name")
        if not isinstance(self.commands, tuple) or not all(
                type(command) is str and _COMMAND.fullmatch(command) for command in self.commands):
            raise ValueError(f"Service function {self.name!r} has an invalid command")
        if self.callback_prefix is not None and (
                type(self.callback_prefix) is not str or not _PREFIX.fullmatch(self.callback_prefix)):
            raise ValueError(f"Service function {self.name!r} has an invalid callback prefix")
        if not self.commands and self.callback_prefix is None:
            raise ValueError(f"Service function {self.name!r} answers nothing")
        if self.audience not in ("anyone", "admitted", "owner"):
            raise ValueError(f"Service function {self.name!r} has an unknown audience")
        if not callable(self.handle):
            raise ValueError(f"Service function {self.name!r} needs a handler")
        if self.text_input is not None and not isinstance(self.text_input, TextInput):
            raise ValueError(f"Service function {self.name!r} has an invalid text input")


class RouterError(RuntimeError):
    """The service modules do not describe a usable set of functions; the bot cannot start."""


START = ServiceFunction("start", ("start",), None, "anyone", send_start)


def command_id(update: Update) -> UUID:
    """The core command ID of an update: the same update always gives the same ID (ВП-6)."""
    if update.callback_query is not None:
        return uuid5(COMMAND_NAMESPACE, f"cbq:{update.callback_query.id}")
    if update.message is not None:
        return uuid5(COMMAND_NAMESPACE, f"msg:{update.message.chat.id}:{update.message.message_id}")
    raise ValueError("Only a new message or a button press is a command")


def _declared(module: Any) -> tuple[ServiceFunction, ...]:
    functions = getattr(module, "SERVICE_FUNCTIONS", None)
    if not isinstance(functions, tuple) or not all(isinstance(item, ServiceFunction) for item in functions):
        raise RouterError(f"Module {module.__name__} declares no SERVICE_FUNCTIONS tuple")
    return functions


def discover(package: str = SERVICE_PACKAGE) -> tuple[ServiceFunction, ...]:
    """Collect the service functions of ``package``."""
    functions: list[ServiceFunction] = []
    root = import_module(package)
    for module in sorted(pkgutil.iter_modules(root.__path__), key=lambda item: item.name):
        if not module.name.startswith("_"):
            functions.extend(_declared(import_module(f"{package}.{module.name}")))
    return tuple(functions)


def _checked(functions: Sequence[ServiceFunction]) -> None:
    commands: set[str] = set()
    prefixes: set[str] = set()
    for function in functions:
        for command in function.commands:
            if command in commands:
                raise RouterError(f"Command /{command} is declared twice")
            commands.add(command)
        if function.callback_prefix is not None:
            if function.callback_prefix in prefixes:
                raise RouterError(f"Callback prefix {function.callback_prefix!r} is declared twice")
            prefixes.add(function.callback_prefix)


async def _admits(function: ServiceFunction, update: Update, context: BotContext) -> bool:
    if function.audience == "anyone":
        return True
    summary = await context.gateway.profile_summary(principal_of(update))
    if summary is None or summary.get("status") != "admitted":
        return False
    return function.audience == "admitted" or summary.get("role") == "owner"


async def answer_anything_else(update: Update, context: BotContext) -> None:
    """ВП-27 (answer 2.7): the /start answer, a short text with the Mini App button."""
    if update.callback_query is not None:
        # Ends the button's loading state; an old button carries no action here.
        await send_reply(update.callback_query.answer, sleep=context.sleep)
    await send_start(update, context)


def _retryable(error: BaseException) -> bool:
    """The core gave no answer (down, slow, failing), as opposed to refusing the contact."""
    return isinstance(error, (CoreUnavailable, TimeoutError))


async def _contact_once(context: BotContext, principal: Any, age_ms: int = 0) -> BaseException | None:
    try:
        # A timed-out call is cancelled with its HTTP request; its result is not needed.
        await asyncio.wait_for(context.gateway.note_contact(principal, age_ms=age_ms), CONTACT_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - best effort by design
        # The class only: the core's message may quote data (TC:273, T1).
        logger.warning("note_contact failed: %s", type(error).__name__)
        return error
    return None


async def _retry_contact(context: BotContext, principal: Any) -> None:
    key = principal.external_id
    # The task began inside an update and copied its context; a retry minutes later is no part of it.
    end_update()
    try:
        pause_first = True
        for pause in CONTACT_RETRY_DELAYS:
            if pause_first:
                await context.sleep(pause)
            # Counted from the newest unanswered contact (QA Н2): the core then dates the
            # return no earlier than it was proven. At most CONTACT_AGE_MAX_MS, the core's
            # bound; the retries end well before it.
            sent_from = context.contact_seen[key]
            age_ms = min(CONTACT_AGE_MAX_MS, max(0, round((context.monotonic() - sent_from) * 1000)))
            error = await _contact_once(context, principal, age_ms)
            if error is None:
                if context.contact_seen[key] <= sent_from:
                    logger.info("note_contact retried: done")
                    return
                # A newer contact failed while this call ran (QA Д2): note it too, at once,
                # since the core answers again; it takes the next place of the same series.
                pause_first = False
                continue
            if not _retryable(error):
                return
            pause_first = True
        logger.warning("note_contact given up after the last retry")
    finally:
        context.contact_retries.pop(key, None)
        context.contact_seen.pop(key, None)


async def note_contact(update: Update, context: BotContext) -> None:
    """Tell the core the user wrote; nothing here may keep the user from an answer."""
    principal = principal_of(update)
    key = principal.external_id
    happened_at = context.monotonic()
    error = await _contact_once(context, principal)
    if error is None or not _retryable(error):
        return
    # A newer unanswered contact moves the running retry's time forward; one retry per user.
    context.contact_seen[key] = max(context.contact_seen.get(key, happened_at), happened_at)
    if key not in context.contact_retries:
        context.contact_retries[key] = asyncio.get_running_loop().create_task(_retry_contact(context, principal))


def _private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == chat.PRIVATE


def _unblocked(update: Update) -> bool:
    """Telegram reports that the user of this private chat unblocked the bot (Я18)."""
    change = update.my_chat_member
    return (change is not None and change.new_chat_member.status == ChatMemberStatus.MEMBER
            and change.from_user.id == change.chat.id)


def _command_or_button(update: Update) -> bool:
    return update.callback_query is not None or (
        update.message is not None and bool(filters.COMMAND.check_update(update)))


async def _take_text(update: Update, context: BotContext,
                     functions: Sequence[ServiceFunction]) -> ServiceFunction | None:
    """The function that took the plain text of ``update``, or None when none did."""
    if update.message is None or filters.COMMAND.check_update(update):
        return None
    for function in functions:
        text_input = function.text_input
        if text_input is None or not text_input.waiting(update):
            continue
        if not await _admits(function, update, context):
            # The sender lost the audience since the input began: the input ends here.
            text_input.cancel(update)
            return None
        await text_input.handle(update, context)
        return function
    return None


async def take_text(update: Update, context: BotContext, functions: Sequence[ServiceFunction]) -> bool:
    """Hand a plain text to the function whose input waits for it; False when none takes it."""
    return await _take_text(update, context, functions) is not None


DEFAULT_FUNCTION = "default"


class _UpdateLine:
    """The ``b.update`` line of one handled update, written once, whatever the handler does."""

    def __init__(self, update: Update, fn: str) -> None:
        self.kind = "callback" if update.callback_query is not None else "message"
        self.fn = fn
        self.outcome = "handled"
        self.err: str | None = None
        self.started = time.monotonic()

    def __enter__(self) -> _UpdateLine:
        return self

    def __exit__(self, kind, error, _traceback) -> None:
        if error is not None:
            self.outcome, self.err = "failed", type(error).__name__
        emit("b.update", component=BOT, trace=update_trace() or begin_update(), kind=self.kind, fn=self.fn,
             outcome=self.outcome, ms=max(0, round((time.monotonic() - self.started) * 1000)), err=self.err,
             lvl="warning" if self.outcome == "failed" else None)


def install(application: Application, context: BotContext, functions: Sequence[ServiceFunction]) -> None:
    """Register the contact note (group -1), /start, the service functions and, last, the default handler.

    An unblock update reaches only the contact note: no handler of group 0 matches it.
    """
    functions = (START, *functions)
    _checked(functions)
    # The closed list of ``fn`` in the trail: the functions of this bot, nothing a user can type.
    allow_functions([function.name for function in functions] + [DEFAULT_FUNCTION])
    new_private_message = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

    def run(function: ServiceFunction):
        async def callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
            with _UpdateLine(update, function.name) as line:
                if not _private(update):
                    line.outcome = "ignored"
                    return
                if await _admits(function, update, context):
                    await function.handle(update, context)
                else:
                    line.outcome = "denied"
                    await answer_anything_else(update, context)
        return callback

    async def contact(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not _private(update):
            return
        if update.message is not None or update.callback_query is not None:
            await note_contact(update, context)
        elif _unblocked(update):
            # No IDs or names (TC:273): the line only shows the event reached the bot.
            logger.info("unblock event received")
            await note_contact(update, context)

    async def interrupt(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        # The first handler of every update: its trace, for the calls of the core and the trail.
        begin_update()
        if _private(update) and _command_or_button(update):
            for function in functions:
                if function.text_input is not None:
                    function.text_input.cancel(update)

    # Groups -2 and -1 run before the default group 0 and do not stop it: one call per update.
    application.add_handler(TypeHandler(Update, interrupt), group=-2)
    application.add_handler(TypeHandler(Update, contact), group=-1)

    for function in functions:
        if function.commands:
            application.add_handler(CommandHandler(function.commands, run(function), filters=new_private_message))
        if function.callback_prefix is not None:
            pattern = f"^{re.escape(function.callback_prefix)}:"
            application.add_handler(CallbackQueryHandler(run(function), pattern=pattern))

    async def anything_else(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        with _UpdateLine(update, DEFAULT_FUNCTION) as line:
            if not _private(update):
                line.outcome = "ignored"
                return
            taker = await _take_text(update, context, functions)
            if taker is None:
                await answer_anything_else(update, context)
            else:
                line.fn = taker.name

    application.add_handler(MessageHandler(new_private_message & filters.TEXT, anything_else))
    application.add_handler(CallbackQueryHandler(anything_else))
