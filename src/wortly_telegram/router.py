"""Which code answers an update: service functions, /start and the default handler (design T-D §7.1, §6).

Service functions are found automatically (ВП-21): every module of
wortly_telegram.service declares ``SERVICE_FUNCTIONS`` (the owner's admission of T2 is
wortly_telegram.service.access). A package adds a module and edits no shared list. A service module without the
declaration, a command or callback prefix claimed twice stops the start.

- Only private chats are served: Telegram accepts a web_app button only there.
- Only new messages and button presses are handled; an edited message is neither
  requested nor handled (ВП-6), because it would repeat the command of its original.
- ``audience`` filters the interface only. The core checks every right again for the
  principal under its locks (§7.1 п.2); the role in a summary is not a right, so an
  owner function also needs an admitted status (QA C-ADAPTER).
- callback_data is ``<prefix>:<action>:<object references>`` and never names the
  actor (§7.1 п.3); the actor is always ``update.effective_user`` (gateway.principal_of).
- ``command_id`` is derived from the update itself (ВП-6), so the same update
  delivered twice (two pollers, a restart) is the same command for the core.

Contact (answer 2.5): any private message or button press of a user proves the bot is not
blocked, so the first handler of every such update (handler group -1, before the service
functions and the default answer) calls ``gateway.note_contact`` exactly once. The call is
best effort: a failure or a timeout (CONTACT_TIMEOUT) is logged by class only and never stops
the answer to the user.
Which events count as "wrote again" is the owner's open question 2.28б; until it is
answered, every private message and button press counts.

Text input (T2, answer 2.6 б): a service function may claim the next plain text of a
chat with ``text_input`` (the owner's reason for a rejection). The router hands a plain
text, not a command, to the first function whose input waits for that chat and whose
audience still admits the sender; otherwise the default answer follows. Every command
and every button press of a private chat first cancels each waiting input (handler group
-2), before any handler runs: until the owner answers Q55 a command or a button ends the
input. A function's own button then starts a new one in its handler.

Default handler — temporary rule ВП-27 until the owner answers Q23: any other text,
an old command (/undo) or an unknown button gets the /start answer, a short text with
the Mini App button (the old bot's handler of orphaned text, R-TG 1.03, led to /start
too). After Q23 only ``answer_anything_else`` in this module changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from importlib import import_module
import logging
import pkgutil
import re
from typing import Any, Literal, Protocol
from uuid import UUID, uuid5

from telegram import Bot, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters,
)

from .gateway import principal_of
from .reply import send_reply
from .start import send_start


logger = logging.getLogger(__name__)

SERVICE_PACKAGE = "wortly_telegram.service"
# What the poller asks Telegram for: exactly the update types handled below (ВП-6).
ALLOWED_UPDATES = ("message", "callback_query")

# PTB handles updates one at a time, so a hanging core call would hold the whole bot;
# the contact note is best effort and is given up after this many seconds.
CONTACT_TIMEOUT = 2.0

# Fixed for good: the same update must give the same command_id in every release.
COMMAND_NAMESPACE = UUID("34f1fd3d-1a7a-4b83-983e-b6478ea6bbc1")

Audience = Literal["anyone", "admitted", "owner"]
_COMMAND = re.compile(r"[a-z0-9_]{1,32}")
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,15}")


class Gateway(Protocol):
    async def note_contact(self, principal: Any) -> None: ...

    async def profile_summary(self, principal: Any) -> dict | None: ...


@dataclass(frozen=True)
class BotContext:
    """What every handler of the bot gets besides the update."""

    bot: Bot
    gateway: Gateway
    web_app_url: str
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)


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
    """ВП-27 (temporary, until Q23): the /start answer, a short text with the Mini App button."""
    if update.callback_query is not None:
        # Ends the button's loading state; an old button carries no action here.
        await send_reply(update.callback_query.answer, sleep=context.sleep)
    await send_start(update, context)


async def note_contact(update: Update, context: BotContext) -> None:
    """Tell the core the user wrote; nothing here may keep the user from an answer."""
    try:
        # A timed-out call is cancelled with its HTTP request; its result is not needed.
        await asyncio.wait_for(context.gateway.note_contact(principal_of(update)), CONTACT_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - best effort by design
        # The class only: the core's message may quote data (TC:273, T1).
        logger.warning("note_contact failed: %s", type(error).__name__)


def _private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == chat.PRIVATE


def _command_or_button(update: Update) -> bool:
    return update.callback_query is not None or (
        update.message is not None and bool(filters.COMMAND.check_update(update)))


async def take_text(update: Update, context: BotContext, functions: Sequence[ServiceFunction]) -> bool:
    """Hand a plain text to the function whose input waits for it; False when none takes it."""
    if update.message is None or filters.COMMAND.check_update(update):
        return False
    for function in functions:
        text_input = function.text_input
        if text_input is None or not text_input.waiting(update):
            continue
        if not await _admits(function, update, context):
            # The sender lost the audience since the input began: the input ends here.
            text_input.cancel(update)
            return False
        await text_input.handle(update, context)
        return True
    return False


def install(application: Application, context: BotContext, functions: Sequence[ServiceFunction]) -> None:
    """Register the contact note (group -1), /start, the service functions and, last, the default handler."""
    functions = (START, *functions)
    _checked(functions)
    new_private_message = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE

    def run(function: ServiceFunction):
        async def callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
            if not _private(update):
                return
            if await _admits(function, update, context):
                await function.handle(update, context)
            else:
                await answer_anything_else(update, context)
        return callback

    async def contact(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if _private(update) and (update.message is not None or update.callback_query is not None):
            await note_contact(update, context)

    async def interrupt(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
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
        if _private(update) and not await take_text(update, context, functions):
            await answer_anything_else(update, context)

    application.add_handler(MessageHandler(new_private_message & filters.TEXT, anything_else))
    application.add_handler(CallbackQueryHandler(anything_else))
