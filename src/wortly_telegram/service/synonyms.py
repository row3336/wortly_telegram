"""The owner's synonym groups in the bot (project DEV-17 ed. 3.3 §3.5, §3.6, §4.4; stage B4).

The old bot's set without the ⚠️ flag (Q07, BE-4): the group's list with ``[✖ i]``, ``[➕ Add]``,
``[Pick by translation]``, ``[Clear the group]`` (with a confirmation, BE-7) and the way back to
the card. Groups join published cards of one type (core C4, dictionary/synonyms.py).

- Audience "owner"; the core checks the right again on every call; the actor is the sender.
- ➕: the owner's next text is only a **read**: the owner's search limited to the card's type.
  The answer is one ``[Link: …]`` button per found card, even for one (§3.5), each with a key
  of the state remembered at that moment: the card, the found card and the versions of both
  groups at the moment of the search (§3.5): the card's own group is read with the search,
  the found card's comes with it. A repeat of the same text writes nothing: it shows buttons
  again (both reads are made again).
- ``sy:pk`` (link) and ``sy:ok`` (link the candidates, clear the group) take their body only
  from that state, and their command_id from the update, so the same press delivered again
  is the same command, even after a merge changed the groups in between (already_applied,
  §4.4). A new press on a stale button meets the core's VERSION_CONFLICT.
- Keys live in this process's memory, the last 20 per chat (§3.6); after a restart a key is
  not found and the press is answered "the confirmation is out of date", with no write.
- ``[✖ i]`` carries the card, the group version and i: it works after a restart.
- Applied: "✅ Saved." and the group as the core answered it; already_applied: the group only.
  A conflict reads the card again and shows its group with the notice.
- Nothing here writes a log line; the trail is the router's, the gateway's and send_reply's.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import secrets
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from telegram import Update

from ..card_view import ANSWERS, NOTICES, View, with_notice
from ..gateway import QUERY_MAX_CHARACTERS, SEARCH_LIMIT_MAX, CoreError, CoreUnavailable, principal_of
from ..messages import Language
from ..reply import send_reply
from ..router import ServiceFunction, TextInput, command_id
from ..start import language_of
from ..synonym_view import (
    ADD, CALLBACK_PREFIX, CANDIDATES, CLEAR, CONFIRM, PICK, REMOVE, SYNONYM_TEXTS, VIEW, candidates_text,
    confirm_view, group_view, parse, pick_view,
)
from ..webapp import buttons_keyboard

if TYPE_CHECKING:
    from ..router import BotContext


# Keys kept per chat (§3.6): the newest ones; an older key is "out of date".
KEYS_PER_CHAT = 20
CONFLICTS = frozenset({"VERSION_CONFLICT", "COMMAND_PAYLOAD_MISMATCH"})


@dataclass(frozen=True)
class Pending:
    """The state behind one ``sy:pk``/``sy:ok`` key: what its command will send, nothing else."""

    kind: Literal["link", "link_many", "clear"]
    card_id: UUID
    version: int | None
    others: tuple[UUID, ...] = ()
    other_versions: tuple[int | None, ...] = ()


class SynonymKeys:
    """The keys of this process, the last KEYS_PER_CHAT per chat; lost on a restart."""

    def __init__(self, make_key: Callable[[], str] = lambda: secrets.token_urlsafe(6)) -> None:
        self._chats: dict[int, OrderedDict[str, Pending]] = {}
        self._make_key = make_key

    def put(self, chat_id: int, pending: Pending) -> str:
        keys = self._chats.setdefault(chat_id, OrderedDict())
        key = self._make_key()
        while key in keys:
            key = self._make_key()
        keys[key] = pending
        while len(keys) > KEYS_PER_CHAT:
            keys.popitem(last=False)
        return key

    def get(self, chat_id: int, key: str) -> Pending | None:
        return self._chats.get(chat_id, {}).get(key)


@dataclass(frozen=True)
class AddInput:
    """➕ awaits a word or a card number of ``card_type`` for ``card_id``."""

    user_id: int
    card_id: UUID
    card_type: str


class AddInputs:
    """The ➕ inputs of this process, one per owner chat; a command or a button ends it (router)."""

    def __init__(self) -> None:
        self._inputs: dict[int, AddInput] = {}

    def start(self, chat_id: int, waiting: AddInput) -> None:
        self._inputs[chat_id] = waiting

    def of(self, update: Update) -> AddInput | None:
        chat, user = update.effective_chat, update.effective_user
        if chat is None or user is None:
            return None
        waiting = self._inputs.get(chat.id)
        return waiting if waiting is not None and waiting.user_id == user.id else None

    def waiting(self, update: Update) -> bool:
        return self.of(update) is not None

    def cancel(self, update: Update) -> None:
        if update.effective_chat is not None:
            self._inputs.pop(update.effective_chat.id, None)


class SynonymEditor:
    def __init__(self, inputs: AddInputs, keys: SynonymKeys) -> None:
        self.inputs = inputs
        self.keys = keys

    # -- replies (as in service/card_editor.py) ------------------------------------------

    @staticmethod
    async def _send(update: Update, context: BotContext, text: str, view: View | None = None) -> None:
        chat_id = update.effective_chat.id
        markup = buttons_keyboard(view.buttons) if view is not None and view.buttons else None
        await send_reply(lambda: context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup),
                         sleep=context.sleep)

    async def _show(self, update: Update, context: BotContext, view: View) -> None:
        """A button's view replaces its own message."""
        message = update.callback_query.message
        markup = buttons_keyboard(view.buttons) if view.buttons else None
        await send_reply(lambda: context.bot.edit_message_text(view.text, chat_id=message.chat.id,
                                                               message_id=message.message_id, reply_markup=markup),
                         sleep=context.sleep)

    async def _refusal(self, update: Update, context: BotContext, error: CoreError, language: Language,
                       *, read: bool) -> None:
        code = None if isinstance(error, CoreUnavailable) else error.code
        answers = ANSWERS[language]
        if code == "FORBIDDEN":
            text = answers["forbidden"]
        elif code == "WRITE_UNAVAILABLE":
            text = answers["card_unavailable"]
        elif code == "VALIDATION_FAILED" and not read:
            text = SYNONYM_TEXTS[language]["refused"]
        else:
            text = NOTICES[language]["read_failed"] if read else answers["core_silent"]
        await self._send(update, context, text)

    async def _group(self, update: Update, context: BotContext, card_id: UUID, language: Language) -> View | None:
        """The card's group now (a read), or None after the refusal was answered."""
        try:
            card = await context.gateway.owner_card(principal_of(update), card_id=card_id)
        except CoreError as error:
            await self._refusal(update, context, error, language, read=True)
            return None
        return group_view(card.card_id, card.card_type, card.synonyms, language)

    # -- buttons -------------------------------------------------------------------------

    async def on_button(self, update: Update, context: BotContext) -> None:
        query = update.callback_query
        await send_reply(query.answer, sleep=context.sleep)
        press = parse(query.data)
        if press is None:
            return
        language = await language_of(update, context)
        chat_id = update.effective_chat.id
        if press.action == VIEW:
            view = await self._group(update, context, press.card_id, language)
            if view is not None:
                await self._show(update, context, view)
        elif press.action == ADD:
            self.inputs.start(chat_id, AddInput(update.effective_user.id, press.card_id, press.card_type))
            await self._send(update, context, SYNONYM_TEXTS[language]["add_prompt"])
        elif press.action == CANDIDATES:
            await self._candidates(update, context, press, language)
        elif press.action == CLEAR:
            key = self.keys.put(chat_id, Pending("clear", press.card_id, press.version))
            view = confirm_view(SYNONYM_TEXTS[language]["clear"], key, press.card_id, language)
            await self._send(update, context, view.text, view)
        elif press.action == REMOVE:
            await self._write(update, context, language, press.card_id, lambda: context.gateway.synonym_unlink(
                principal_of(update), command_id=command_id(update), card_id=press.card_id, index=press.index,
                expected_group_version=press.version))
        else:
            pending = self.keys.get(chat_id, press.key)
            if pending is None or (press.action == PICK) != (pending.kind == "link"):
                # After a restart, or a key pushed out by 20 newer ones (§3.6): nothing is written.
                await self._send(update, context, SYNONYM_TEXTS[language]["stale"])
                return
            await self._write(update, context, language, pending.card_id, self._command(update, context, pending))

    def _command(self, update: Update, context: BotContext, pending: Pending) -> Callable[[], Awaitable]:
        """The command of a key: its body is the remembered state only (§4.4)."""
        gateway, principal, command = context.gateway, principal_of(update), command_id(update)
        if pending.kind == "link":
            return lambda: gateway.synonym_link(
                principal, command_id=command, card_id=pending.card_id, other_card_id=pending.others[0],
                expected_group_version=pending.version, expected_other_group_version=pending.other_versions[0])
        if pending.kind == "link_many":
            return lambda: gateway.synonym_link_many(
                principal, command_id=command, card_id=pending.card_id, other_card_ids=pending.others,
                expected_group_version=pending.version)
        return lambda: gateway.synonym_clear(principal, command_id=command, card_id=pending.card_id,
                                             expected_group_version=pending.version)

    async def _candidates(self, update: Update, context: BotContext, press, language: Language) -> None:
        """"Pick by translation": the candidates (a read), then ``[Confirm] [Cancel]`` (§3.5)."""
        try:
            found = await context.gateway.synonym_candidates(principal_of(update), card_id=press.card_id)
        except CoreError as error:
            await self._refusal(update, context, error, language, read=True)
            return
        if not found.cards:
            await self._send(update, context, SYNONYM_TEXTS[language]["no_candidates"])
            return
        key = self.keys.put(update.effective_chat.id, Pending(
            "link_many", press.card_id, press.version, tuple(card.card_id for card in found.cards)))
        view = confirm_view(candidates_text(found.cards, found.more, language), key, press.card_id, language)
        await self._send(update, context, view.text, view)

    async def _write(self, update: Update, context: BotContext, language: Language, card_id: UUID,
                     call: Callable[[], Awaitable]) -> None:
        texts = SYNONYM_TEXTS[language]
        try:
            result = await call()
        except CoreError as error:
            if not isinstance(error, CoreUnavailable) and error.code in CONFLICTS:
                view = await self._group(update, context, card_id, language)
                if view is not None:
                    await self._show(update, context, with_notice(texts["conflict"], view))
                return
            await self._refusal(update, context, error, language, read=False)
            return
        if result.current is None:
            # A repeat after the actor stopped being the owner (§4.4, ed. 3.3): nothing is shown.
            return
        group = result.current
        view = group_view(group.card_id, group.card_type, group.synonyms, language)
        await self._show(update, context, with_notice(ANSWERS[language]["saved"], view)
                         if result.status == "applied" else view)

    # -- ➕ text: a read only --------------------------------------------------------------

    async def on_text(self, update: Update, context: BotContext) -> None:
        waiting = self.inputs.of(update)
        language = await language_of(update, context)
        texts = SYNONYM_TEXTS[language]
        query = (update.message.text or "").strip()
        if not query or len(query) > QUERY_MAX_CHARACTERS:
            await self._send(update, context, texts["add_prompt"])
            return
        principal = principal_of(update)
        try:
            # Both groups at the moment of the search (§3.5): the card's own now, the found ones with them.
            own = (await context.gateway.owner_card(principal, card_id=waiting.card_id)).synonyms
            found = await context.gateway.owner_search(principal, query=query, limit=SEARCH_LIMIT_MAX,
                                                       card_type=waiting.card_type)
        except CoreError as error:
            if error.code == "VALIDATION_FAILED" and not isinstance(error, CoreUnavailable):
                await self._send(update, context, texts["not_found"])
                return
            await self._refusal(update, context, error, language, read=True)
            return
        if not found.cards:
            await self._send(update, context, texts["not_found"])
            return
        # The card itself and the cards already in its group are no choice.
        linked = {waiting.card_id, *(member.card_id for member in own.members)}
        choices = [card for card in found.cards if card.card_id not in linked]
        if not choices:
            await self._send(update, context, texts["all_in_group"])
            return
        chat_id = update.effective_chat.id
        keyed = [(card, self.keys.put(chat_id, Pending("link", waiting.card_id, own.version, (card.card_id,),
                                                       (card.group_version,))))
                 for card in choices]
        view = pick_view(keyed, found.more, language)
        await self._send(update, context, view.text, view)


def service_functions(inputs: AddInputs, keys: SynonymKeys) -> tuple[ServiceFunction, ...]:
    editor = SynonymEditor(inputs, keys)
    return (ServiceFunction("synonyms", (), CALLBACK_PREFIX, "owner", editor.on_button,
                            text_input=TextInput(inputs.waiting, editor.on_text, inputs.cancel)),)


SERVICE_FUNCTIONS = service_functions(AddInputs(), SynonymKeys())
