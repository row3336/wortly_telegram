"""The owner's common editor in the bot: /find, the card, its translations and grammar (DEV-17 B2, B3).

Project DEV-17 ed. 3.3 §3.1-§3.4, §3.6, §3.7, §4.4; R12-05 (the same set as the Mini App:
translations and grammar; synonyms are stage B4), R14-01 (one translation per edit, the
order by up/down), R12-06 (the last translation of a language is not removed), R9-05 (the
copyable echo before an input), Q13 (/find), owner's answers R17-01 and R17-02 (variant 1
each, 15.10.2026: no unpublished cards; every language editable, the others folded).

- Audience "owner": the router answers anyone else with /start (ВП-27); the core checks the
  right again on every call. The actor is always the sender of the update (principal_of).
- Reads (search, card, a list or grammar view, the echo before an input) are separate steps
  that end with a view. A write never reads first: its body comes from the button's data,
  or from the input remembered at the press plus the message's text, and its command_id
  from the update (router.command_id). The same update twice is the same command and the
  core answers already_applied (§4.4). After a write the view is redrawn from the core's
  answer: "✅ Saved." only for applied. VERSION_CONFLICT (and COMMAND_PAYLOAD_MISMATCH, which
  the construction makes unreachable) read the card afresh and redraw it with the notice.
- A button's view is redrawn in its own message; a text input's in a new message.
- Text input (router.TextInput): ✏️, ➕, "Enter a form" and /find without a query wait for
  the owner's next plain text in this chat, in this process's memory. A command or a button
  ends it (router). After a write the input stays bound to the message that made it: the
  same message delivered again repeats the same command, any other text gets /start. After a
  restart a text finds no input and gets /start without a write (§3.6).
- Nothing here writes a log line; the trail (b.update, b.core, b.reply) is the router's,
  the gateway's and send_reply's, with no chat, name, text or button data.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from telegram import Update

from ..card_view import (
    ADD, ANSWERS, CALLBACK_PREFIX, CARD, EDIT, ENTER, FIELD, FIELD_LABELS, GRAMMAR, GRAMMAR_FIELDS, LIST,
    LIST_COMMANDS, NOTICES,
    OPEN, REMOVE, VALUE, View, card_view, current_form, field_view, grammar_view, list_view, parse, search_view,
    with_notice,
)
from ..echo import send_echo
from ..gateway import QUERY_MAX_CHARACTERS, SEARCH_LIMIT_MAX, CoreError, CoreUnavailable, OwnerCard, principal_of
from ..messages import Language
from ..reply import send_reply
from ..router import ServiceFunction, TextInput, command_id
from ..start import language_of
from ..webapp import buttons_keyboard

if TYPE_CHECKING:
    from ..router import BotContext


FIND = "find"
CONFLICTS = frozenset({"VERSION_CONFLICT", "COMMAND_PAYLOAD_MISMATCH"})


@dataclass(frozen=True)
class EditorInput:
    """What the owner's next text in this chat is for, as the press remembered it (§4.4).

    ``kind`` is "search" (a word or a card number), "translation" (``op`` replace at
    ``index`` or append, in ``language``) or "grammar" (the form of ``field_name``), each at
    ``revision`` of ``card_id``. ``used`` is the message that made the write, once made.
    """

    user_id: int
    kind: Literal["search", "translation", "grammar"]
    card_id: UUID | None = None
    revision: int | None = None
    language: str | None = None
    op: str | None = None
    index: int | None = None
    field_name: str | None = None
    used: int | None = None


class EditorInputs:
    """The inputs awaited in this process, one per owner chat; lost on a restart (§3.6)."""

    def __init__(self) -> None:
        self._inputs: dict[int, EditorInput] = {}

    def start(self, chat_id: int, waiting: EditorInput) -> None:
        self._inputs[chat_id] = waiting

    def of(self, update: Update) -> EditorInput | None:
        """The input of this sender's chat; after a write only the message that made it."""
        chat, user, message = update.effective_chat, update.effective_user, update.message
        if chat is None or user is None:
            return None
        waiting = self._inputs.get(chat.id)
        if waiting is None or waiting.user_id != user.id:
            return None
        if waiting.used is not None and (message is None or message.message_id != waiting.used):
            return None
        return waiting

    def waiting(self, update: Update) -> bool:
        return self.of(update) is not None

    def cancel(self, update: Update) -> None:
        if update.effective_chat is not None:
            self._inputs.pop(update.effective_chat.id, None)

    def bind(self, update: Update, waiting: EditorInput) -> None:
        """Keep the input for repeats of this message only: the write was made."""
        self._inputs[update.effective_chat.id] = replace(waiting, used=update.message.message_id)


def _one_line(text: str) -> bool:
    return len(text.strip().splitlines()) == 1


class CardEditor:
    def __init__(self, inputs: EditorInputs) -> None:
        self.inputs = inputs

    # -- replies -------------------------------------------------------------------------

    @staticmethod
    async def _send(update: Update, context: BotContext, text: str, view: View | None = None) -> None:
        chat_id = update.effective_chat.id
        markup = buttons_keyboard(view.buttons) if view is not None and view.buttons else None
        await send_reply(lambda: context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup),
                         sleep=context.sleep)

    async def _show(self, update: Update, context: BotContext, view: View) -> None:
        """A button's view replaces its own message; any other goes as a new message."""
        query = update.callback_query
        if query is None or query.message is None:
            await self._send(update, context, view.text, view)
            return
        message = query.message
        markup = buttons_keyboard(view.buttons) if view.buttons else None
        await send_reply(lambda: context.bot.edit_message_text(view.text, chat_id=message.chat.id,
                                                               message_id=message.message_id, reply_markup=markup),
                         sleep=context.sleep)

    async def _refusal(self, update: Update, context: BotContext, error: CoreError, language: Language,
                       *, read: bool) -> None:
        """The answer to a refusal of the core that redraws nothing (§3.7)."""
        answers, code = ANSWERS[language], None if isinstance(error, CoreUnavailable) else error.code
        if code == "FORBIDDEN":
            text = answers["forbidden"]
        elif code == "WRITE_UNAVAILABLE":
            # 409: the card is absent or no longer published (a 503 is CoreUnavailable).
            text = answers["card_unavailable"]
        elif code == "VALIDATION_FAILED" and not read:
            text = answers["validation"]
        else:
            # No answer, or a code the editor does not expect. After a command the change may
            # have been saved; a read only failed.
            text = NOTICES[language]["read_failed"] if read else answers["core_silent"]
        await self._send(update, context, text)

    async def _read(self, update: Update, context: BotContext, card_id: UUID, language: Language) -> OwnerCard | None:
        """The card now, or None after the refusal was answered."""
        try:
            return await context.gateway.owner_card(principal_of(update), card_id=card_id)
        except CoreError as error:
            await self._refusal(update, context, error, language, read=True)
            return None

    # -- entry points --------------------------------------------------------------------

    async def handle(self, update: Update, context: BotContext) -> None:
        if update.callback_query is not None:
            await self.on_button(update, context)
        else:
            await self.on_find(update, context)

    async def on_find(self, update: Update, context: BotContext) -> None:
        """/find <word or number>; without one the next text is the query (§3.1)."""
        language = await language_of(update, context)
        parts = (update.message.text or "").split(maxsplit=1)
        query = parts[1].strip() if len(parts) > 1 else ""
        if not query:
            self.inputs.start(update.effective_chat.id, EditorInput(update.effective_user.id, "search"))
            await self._send(update, context, NOTICES[language]["search_prompt"])
            return
        await self._search(update, context, query, language)

    async def _search(self, update: Update, context: BotContext, query: str, language: Language) -> None:
        notices = NOTICES[language]

        async def ask_again(key: str) -> None:
            # The next text of this chat is the next query.
            self.inputs.start(update.effective_chat.id, EditorInput(update.effective_user.id, "search"))
            await self._send(update, context, notices[key])
        if not query:
            await ask_again("search_prompt")
            return
        if len(query) > QUERY_MAX_CHARACTERS:
            await ask_again("query_too_long")
            return
        try:
            found = await context.gateway.owner_search(principal_of(update), query=query, limit=SEARCH_LIMIT_MAX)
        except CoreError as error:
            if error.code == "VALIDATION_FAILED" and not isinstance(error, CoreUnavailable):
                # A query the core cannot search (a NUL character and the like): as nothing found.
                await ask_again("not_found")
                return
            self.inputs.cancel(update)
            await self._refusal(update, context, error, language, read=True)
            return
        if not found.cards:
            await ask_again("not_found")
            return
        self.inputs.cancel(update)
        view = search_view(found.cards, found.more, language)
        await self._send(update, context, view.text, view)

    async def on_button(self, update: Update, context: BotContext) -> None:
        query = update.callback_query
        # Ends the button's loading state; the answer follows as a message.
        await send_reply(query.answer, sleep=context.sleep)
        press = parse(query.data)
        if press is None:
            # A malformed or foreign "ce:" button does nothing.
            return
        language = await language_of(update, context)
        if press.action in (OPEN, CARD, LIST, GRAMMAR):
            card = await self._read(update, context, press.card_id, language)
            if card is None:
                return
            if press.action == LIST:
                view = list_view(card, press.language, language)
            elif press.action == GRAMMAR:
                view = grammar_view(card, language)
            else:
                view = card_view(card, language)
            if press.action == OPEN:
                await self._send(update, context, view.text, view)
            else:
                await self._show(update, context, view)
        elif press.action == EDIT:
            await self._edit_prompt(update, context, press, language)
        elif press.action == ADD:
            self.inputs.start(update.effective_chat.id, EditorInput(
                update.effective_user.id, "translation", press.card_id, press.revision, press.language, "append"))
            await self._send(update, context, ANSWERS[language]["append_prompt"])
        elif press.action in LIST_COMMANDS:
            if press.action == REMOVE and press.count == 1:
                # R12-06: the last translation of a language is never removed; the core would refuse too.
                await self._send(update, context, ANSWERS[language]["last_translation"])
                return
            await self._write(update, context, language, press.card_id, lambda: context.gateway.translation_op(
                principal_of(update), command_id=command_id(update), card_id=press.card_id, language=press.language,
                op=LIST_COMMANDS[press.action], index=press.index, text=None,
                expected_card_revision=press.revision), lambda card: list_view(card, press.language, language))
        elif press.action == FIELD:
            await self._field(update, context, press, language)
        elif press.action == ENTER:
            self.inputs.start(update.effective_chat.id, EditorInput(
                update.effective_user.id, "grammar", press.card_id, press.revision, field_name=press.field_name))
            await self._send(update, context, NOTICES[language]["form_prompt"].format(
                field=FIELD_LABELS[language][press.field_name]))
        elif press.action == VALUE:
            await self._write(update, context, language, press.card_id, lambda: context.gateway.grammar_save(
                principal_of(update), command_id=command_id(update), card_id=press.card_id,
                field_name=press.field_name, value=press.value, expected_card_revision=press.revision),
                lambda card: grammar_view(card, language))

    # -- reads before an input -----------------------------------------------------------

    async def _stale(self, update: Update, context: BotContext, view: View, language: Language) -> None:
        await self._show(update, context, with_notice(ANSWERS[language]["conflict"], view))

    async def _edit_prompt(self, update: Update, context: BotContext, press, language: Language) -> None:
        """✏️ i: the echo of translation i, then the prompt; the input remembers language, revision and i."""
        card = await self._read(update, context, press.card_id, language)
        if card is None:
            return
        texts = card.translations[press.language]
        if card.revision != press.revision or press.index > len(texts):
            await self._stale(update, context, list_view(card, press.language, language), language)
            return
        self.inputs.start(update.effective_chat.id, EditorInput(
            update.effective_user.id, "translation", press.card_id, press.revision, press.language, "replace",
            press.index))
        await send_echo(context.bot, update.effective_chat.id, texts[press.index - 1], language, sleep=context.sleep)
        await self._send(update, context, ANSWERS[language]["translation_prompt"].format(index=press.index))

    async def _field(self, update: Update, context: BotContext, press, language: Language) -> None:
        """A grammar field: the echo of its form (a text field), then its value buttons (§3.4)."""
        card = await self._read(update, context, press.card_id, language)
        if card is None:
            return
        if press.field_name not in GRAMMAR_FIELDS.get(card.card_type, ()):
            # A stale or forged button: the field is not one of this card's type. Nothing is
            # written; the owner gets the current view, as for a stale revision.
            view = grammar_view(card, language) if card.card_type in GRAMMAR_FIELDS else card_view(card, language)
            await self._show(update, context, with_notice(NOTICES[language]["stale_button"], view))
            return
        if card.revision != press.revision:
            await self._stale(update, context, grammar_view(card, language), language)
            return
        form = current_form(card, press.field_name)
        if form is not None:
            await send_echo(context.bot, update.effective_chat.id, form, language, sleep=context.sleep)
        view = field_view(card, press.field_name, language)
        await self._send(update, context, view.text, view)

    # -- writes --------------------------------------------------------------------------

    async def _write(self, update: Update, context: BotContext, language: Language, card_id: UUID,
                     call: Callable[[], Awaitable], redraw: Callable[[OwnerCard], View],
                     waiting: EditorInput | None = None) -> None:
        """One command and its answer (§3.7); the body was fixed before this call (§4.4)."""
        answers = ANSWERS[language]
        try:
            result = await call()
        except CoreError as error:
            if not isinstance(error, CoreUnavailable) and error.code in CONFLICTS:
                if waiting is not None:
                    self.inputs.cancel(update)
                card = await self._read(update, context, card_id, language)
                if card is not None:
                    await self._show(update, context, with_notice(answers["conflict"], redraw(card)))
                return
            if waiting is not None and error.code in ("FORBIDDEN", "WRITE_UNAVAILABLE") and not isinstance(
                    error, CoreUnavailable):
                self.inputs.cancel(update)
            # VALIDATION_FAILED and no answer keep a text input: the owner may send another text.
            await self._refusal(update, context, error, language, read=False)
            return
        if waiting is not None:
            self.inputs.bind(update, waiting)
        if result.current is None:
            # A repeat after the actor stopped being the owner (§4.4, ed. 3.3): nothing is shown.
            return
        view = redraw(result.current)
        await self._show(update, context, with_notice(answers["saved"], view) if result.status == "applied" else view)

    async def on_text(self, update: Update, context: BotContext) -> None:
        waiting = self.inputs.of(update)
        language = await language_of(update, context)
        text = update.message.text or ""
        if waiting.kind == "search":
            await self._search(update, context, text.strip(), language)
            return
        if not _one_line(text):
            key = "one_line" if waiting.kind == "translation" else "form_one_line"
            await self._send(update, context, (ANSWERS if key == "one_line" else NOTICES)[language][key])
            return
        principal = principal_of(update)
        if waiting.kind == "translation":
            await self._write(update, context, language, waiting.card_id, lambda: context.gateway.translation_op(
                principal, command_id=command_id(update), card_id=waiting.card_id, language=waiting.language,
                op=waiting.op, index=waiting.index, text=text, expected_card_revision=waiting.revision),
                lambda card: list_view(card, waiting.language, language), waiting)
        else:
            await self._write(update, context, language, waiting.card_id, lambda: context.gateway.grammar_save(
                principal, command_id=command_id(update), card_id=waiting.card_id, field_name=waiting.field_name,
                value={"status": "applicable", "value": text}, expected_card_revision=waiting.revision),
                lambda card: grammar_view(card, language), waiting)


def service_functions(inputs: EditorInputs) -> tuple[ServiceFunction, ...]:
    editor = CardEditor(inputs)
    return (ServiceFunction("card_editor", (FIND,), CALLBACK_PREFIX, "owner", editor.handle,
                            text_input=TextInput(inputs.waiting, editor.on_text, inputs.cancel)),)


SERVICE_FUNCTIONS = service_functions(EditorInputs())
