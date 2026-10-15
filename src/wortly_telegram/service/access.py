"""The owner's admission in the bot: decide requests, reject with a reason, list pending ones (answer 2.6).

Design T-D §5.2, §5.3 (Б5, Р3, Р5); as the old bot (OLD bot/handlers/access.py:64–90,
R-TG 2.07, 4.03, 4.14). Buttons come from the access_request notice, the "show requests"
button of the access_reminder digest and the list below; callback_data is
``acc:<action>:<request_id>:<version>`` or ``acc:l`` and names no actor.

- "Approve" and "Reject" call gateway.decide with the sender of the update as the actor
  (principal_of) and the router's command_id; the core checks the owner's right again.
  Applied: the pressed message gets the outcome and loses its buttons. already_applied
  (the same update twice): nothing more is sent. VERSION_CONFLICT: "already handled" and
  the buttons go; the internal API tells nothing more of a decided request. FORBIDDEN:
  "not allowed". No answer of the core: "try again later" (§5.3 п.2).
- A request whose applicant deleted the profile is gone from the core (P04 §3.3, PD-19): the
  repeat of the same command_id is already_applied with current ``{"status": "unavailable"}``
  and the owner is told the request is no longer available (the buttons go); a new press on it
  is FORBIDDEN, answered like any FORBIDDEN, and nothing is written. The bot cannot tell that
  FORBIDDEN from a refusal to a non-owner: the core gives the same code.
- "Reject with a reason" asks for the reason; the owner's next plain text in this chat is
  the reason (router.TextInput). The input lives in this process's memory, one per
  owner chat, and is lost on a restart: the owner presses the button again. Q55 (approved by R12-14): a
  command or a button cancels it, a text over REASON_MAX_CHARACTERS code points is not
  taken and the bot asks for a shorter one, and the input has no time limit. A reason the
  core still refuses (422 VALIDATION_FAILED) keeps the input and asks for another wording.
- "Show requests": gateway.pending_requests, then one message per request with its
  decision buttons, the same line as the reminder's.

Q31 (tell the applicant about a rejection) is open: nothing is sent to the applicant here.
Nothing here writes a log line: names, usernames and reasons never reach one (TC:273).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

from telegram import Update

from ..gateway import CoreError, CoreUnavailable, is_version, principal_of
from ..messages import Language
from ..messages.access_reminder import count_line, request_line
from ..messages.access_request import (
    APPROVE, CALLBACK_PREFIX, REJECT, REJECT_WITH_REASON, SHOW_REQUESTS, applicant_label, decision_buttons,
)
from ..reply import send_reply
from ..router import ServiceFunction, TextInput, command_id
from ..start import language_of
from ..webapp import buttons_keyboard

if TYPE_CHECKING:
    from ..router import BotContext


# Answer 2.6 г; the core's own bound (identity/service.py REASON_MAX_CHARACTERS, C-ADAPTER-3),
# in code points of Unicode, never bytes.
REASON_MAX_CHARACTERS = 500

# WORKING TEXT (not set by the specification): the old bot's text (answer 2.7; OLD
# bot/i18n.py "access_granted_owner"); final wording waits for the owner (T-TEXTS-PAGE).
APPROVED: dict[Language, str] = {"ru": "✅ Доступ выдан", "uk": "✅ Доступ надано", "en": "✅ Access granted"}
# WORKING TEXT (not set by the specification): the old bot's text (answer 2.7; OLD
# bot/i18n.py "access_rejected_owner"); final wording waits for the owner (T-TEXTS-PAGE).
REJECTED: dict[Language, str] = {"ru": "❌ Отклонён", "uk": "❌ Відхилено", "en": "❌ Rejected"}
# WORKING TEXT (not set by the specification): the old bot's text (answer 2.7; OLD
# bot/i18n.py "access_already_handled"); final wording waits for the owner (T-TEXTS-PAGE).
ALREADY_HANDLED: dict[Language, str] = {"ru": "ℹ️ Уже обработано", "uk": "ℹ️ Уже оброблено",
                                        "en": "ℹ️ Already handled"}
# WORKING TEXT (not set by the specification): the answer to the repeat of a decision on a request that is gone
# with its applicant's deleted profile (P04 §3.3, PD-19), new (the old bot had none);
# final wording waits for the owner (T-TEXTS-PAGE).
REQUEST_GONE: dict[Language, str] = {"ru": "ℹ️ Заявка больше недоступна", "uk": "ℹ️ Заявка більше недоступна",
                                     "en": "ℹ️ The request is no longer available"}
# WORKING TEXT (not set by the specification): the old bot's text (answer 2.7; OLD
# bot/i18n.py "access_no_pending"); final wording waits for the owner (T-TEXTS-PAGE).
NO_PENDING: dict[Language, str] = {"ru": "Нет заявок в ожидании.", "uk": "Немає заявок в очікуванні.",
                                   "en": "No pending requests."}
# WORKING TEXT (not set by the specification): the reason prompt (answer 2.6 б, г; Q55 approved by R12-14), new (the old bot had none);
# final wording waits for the owner (T-TEXTS-PAGE).
REASON_PROMPT: dict[Language, str] = {
    "ru": "✍️ Напишите причину отказа одним сообщением (до 500 символов). Команда или кнопка отменяет ввод.",
    "uk": "✍️ Напишіть причину відмови одним повідомленням (до 500 символів). Команда або кнопка скасовує введення.",
    "en": "✍️ Type the reason for the rejection in one message (up to 500 characters). "
          "A command or a button cancels it.",
}
# WORKING TEXT (not set by the specification): the refusal of a reason over 500 characters (answer 2.6 г; Q55 approved by R12-14), new (the old bot had none);
# final wording waits for the owner (T-TEXTS-PAGE).
REASON_TOO_LONG: dict[Language, str] = {
    "ru": "Причина длиннее 500 символов ({count}). Напишите короче.",
    "uk": "Причина довша за 500 символів ({count}). Напишіть коротше.",
    "en": "The reason is longer than 500 characters ({count}). Please make it shorter.",
}
# WORKING TEXT (not set by the specification): the answer when the core refuses a reason within 500 characters (422), new (the old bot had none);
# final wording waits for the owner (T-TEXTS-PAGE).
REASON_REFUSED: dict[Language, str] = {
    "ru": "Причина не принята ядром, попробуйте другую формулировку.",
    "uk": "Причину не прийнято ядром, спробуйте інше формулювання.",
    "en": "The core did not accept the reason; please try another wording.",
}
# WORKING TEXT (not set by the specification): the reason line of the outcome, new (the old bot had none);
# final wording waits for the owner (T-TEXTS-PAGE).
REASON_LINE: dict[Language, str] = {"ru": "Причина: {reason}", "uk": "Причина: {reason}",
                                    "en": "Reason: {reason}"}
# WORKING TEXT (not set by the specification): the answer to the core's FORBIDDEN (§5.3 п.2), new (the old bot had none);
# final wording waits for the owner (T-TEXTS-PAGE).
FORBIDDEN: dict[Language, str] = {"ru": "⛔ Нет прав.", "uk": "⛔ Немає прав.", "en": "⛔ Not allowed."}
# WORKING TEXT (not set by the specification): the answer when the core is unavailable (§5.3 п.2), new (the old bot had none);
# final wording waits for the owner (T-TEXTS-PAGE).
TRY_LATER: dict[Language, str] = {
    "ru": "Сервис недоступен, повторите позже.",
    "uk": "Сервіс недоступний, повторіть пізніше.",
    "en": "The service is unavailable, please try again later.",
}


@dataclass(frozen=True)
class Decision:
    """A request and the version a decision on it expects, as a button names them."""

    request_id: UUID
    version: int


@dataclass(frozen=True)
class ReasonInput:
    """An owner's reason awaited for ``decision``; the pressed message gets the outcome."""

    user_id: int
    decision: Decision
    chat_id: int = field(repr=False)
    message_id: int = field(repr=False)
    message_text: str | None = field(repr=False)


class ReasonInputs:
    """The reasons awaited in this process, one per owner chat (design §5.2 Р5, variant а)."""

    def __init__(self) -> None:
        self._inputs: dict[int, ReasonInput] = {}

    def start(self, chat_id: int, waiting: ReasonInput) -> None:
        self._inputs[chat_id] = waiting

    def of(self, update: Update) -> ReasonInput | None:
        """The input of this sender's chat; a text of anyone else is never a reason."""
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


def parse(data: str | None) -> tuple[str, Decision | None] | None:
    """The action and the request of a button of this module, or None for anything else."""
    parts = (data or "").split(":")
    if parts == [CALLBACK_PREFIX, SHOW_REQUESTS]:
        return SHOW_REQUESTS, None
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX or parts[1] not in (APPROVE, REJECT, REJECT_WITH_REASON):
        return None
    try:
        request_id = UUID(parts[2])
    except ValueError:
        return None
    if not is_version(parts[3]):
        return None
    return parts[1], Decision(request_id, int(parts[3]))


def outcome_text(approve: bool, reason: str | None, language: Language) -> str:
    text = APPROVED[language] if approve else REJECTED[language]
    return text if reason is None else f"{text}\n{REASON_LINE[language].format(reason=reason)}"


def _with(original: str | None, addition: str) -> str:
    return addition if not original else f"{original}\n\n{addition}"


class Outcome(Enum):
    """What the core made of one decision, as the owner is told it."""

    APPLIED = "applied"
    REPEATED = "already_applied"
    GONE = "request_gone"
    CONFLICT = "version_conflict"
    FORBIDDEN = "forbidden"
    REASON_REFUSED = "reason_refused"
    UNAVAILABLE = "unavailable"


async def _decide(update: Update, context: BotContext, decision: Decision, *, approve: bool,
                  reason: str | None) -> Outcome:
    """Ask the core; the outcome class. The actor is always the sender of this update."""
    try:
        answer = await context.gateway.decide(
            principal_of(update), command_id=command_id(update), request_id=decision.request_id,
            expected_version=decision.version, approve=approve, reason=reason)
    except CoreUnavailable:
        return Outcome.UNAVAILABLE
    except CoreError as error:
        return {"VERSION_CONFLICT": Outcome.CONFLICT, "FORBIDDEN": Outcome.FORBIDDEN,
                "VALIDATION_FAILED": Outcome.REASON_REFUSED if reason is not None else Outcome.UNAVAILABLE,
                }.get(error.code, Outcome.UNAVAILABLE)
    command = answer.get("command") if isinstance(answer, dict) else None
    status = command.get("status") if isinstance(command, dict) else None
    if status == "applied":
        return Outcome.APPLIED
    if status == "already_applied":
        # The repeat of a decision on a request that went with its applicant's profile (P04 §3.3).
        return Outcome.GONE if answer.get("current") == {"status": "unavailable"} else Outcome.REPEATED
    return Outcome.UNAVAILABLE


async def _edit(context: BotContext, chat_id: int, message_id: int, text: str) -> None:
    """Put the outcome on the pressed message and drop its buttons.

    Telegram may refuse the edit (a deleted or too old message: BadRequest); send_reply logs
    the refusal and the owner's answer goes on regardless.
    """
    await send_reply(lambda: context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                                           reply_markup=None), sleep=context.sleep)


async def _say(update: Update, context: BotContext, text: str, markup=None) -> None:
    chat_id = update.effective_chat.id
    await send_reply(lambda: context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup),
                     sleep=context.sleep)


def _pressed(update: Update) -> tuple[int, int, str | None]:
    message = update.callback_query.message
    # A message older than Telegram keeps for bots is inaccessible: it has no text.
    return message.chat.id, message.message_id, getattr(message, "text", None)


class AccessService:
    def __init__(self, inputs: ReasonInputs) -> None:
        self.inputs = inputs

    async def on_button(self, update: Update, context: BotContext) -> None:
        query = update.callback_query
        parsed = parse(query.data)
        if parsed is None:
            # A malformed or foreign "acc:" button decides nothing; the press is acknowledged only.
            await send_reply(query.answer, sleep=context.sleep)
            return
        action, decision = parsed
        language = await language_of(update, context)
        if action == SHOW_REQUESTS:
            await self._show_requests(update, context, language)
        elif action == REJECT_WITH_REASON:
            chat_id, message_id, text = _pressed(update)
            self.inputs.start(chat_id, ReasonInput(update.effective_user.id, decision, chat_id, message_id, text))
            await send_reply(query.answer, sleep=context.sleep)
            await _say(update, context, REASON_PROMPT[language])
        else:
            await self._on_decision_button(update, context, decision, action == APPROVE, language)

    async def _on_decision_button(self, update: Update, context: BotContext, decision: Decision, approve: bool,
                                  language: Language) -> None:
        query = update.callback_query
        outcome = await _decide(update, context, decision, approve=approve, reason=None)
        notice = {Outcome.FORBIDDEN: FORBIDDEN[language], Outcome.UNAVAILABLE: TRY_LATER[language],
                  Outcome.GONE: REQUEST_GONE[language]}.get(outcome)
        await send_reply(lambda: query.answer(notice), sleep=context.sleep)
        chat_id, message_id, text = _pressed(update)
        if outcome == Outcome.APPLIED:
            await _edit(context, chat_id, message_id, _with(text, outcome_text(approve, None, language)))
        elif outcome == Outcome.CONFLICT:
            await _edit(context, chat_id, message_id, _with(text, ALREADY_HANDLED[language]))
        elif outcome == Outcome.GONE:
            await _edit(context, chat_id, message_id, _with(text, REQUEST_GONE[language]))

    async def on_reason(self, update: Update, context: BotContext) -> None:
        waiting = self.inputs.of(update)
        language = await language_of(update, context)
        reason = update.message.text
        if len(reason) > REASON_MAX_CHARACTERS:
            await _say(update, context, REASON_TOO_LONG[language].format(count=len(reason)))
            return
        outcome = await _decide(update, context, waiting.decision, approve=False, reason=reason)
        if outcome == Outcome.REASON_REFUSED:
            # The local bound passed, so the core refused the reason for another rule.
            await _say(update, context, REASON_REFUSED[language])
            return
        if outcome == Outcome.UNAVAILABLE:
            # The input stays: the owner may send the reason again or cancel it.
            await _say(update, context, TRY_LATER[language])
            return
        self.inputs.cancel(update)
        if outcome == Outcome.REPEATED:
            return
        if outcome == Outcome.FORBIDDEN:
            await _say(update, context, FORBIDDEN[language])
            return
        result = (outcome_text(False, reason, language) if outcome == Outcome.APPLIED
                  else REQUEST_GONE[language] if outcome == Outcome.GONE else ALREADY_HANDLED[language])
        await _edit(context, waiting.chat_id, waiting.message_id, _with(waiting.message_text, result))
        await _say(update, context, result)

    async def _show_requests(self, update: Update, context: BotContext, language: Language) -> None:
        query = update.callback_query
        try:
            requests = await context.gateway.pending_requests(principal_of(update))
        except CoreError as error:
            notice = FORBIDDEN[language] if error.code == "FORBIDDEN" else TRY_LATER[language]
            await send_reply(lambda: query.answer(notice), sleep=context.sleep)
            return
        await send_reply(query.answer, sleep=context.sleep)
        if not requests:
            await _say(update, context, NO_PENDING[language])
            return
        await _say(update, context, count_line(len(requests), language))
        for request in requests:
            label = applicant_label(request.telegram_user_id, request.first_name, request.username)
            await _say(update, context, request_line(label),
                       buttons_keyboard(decision_buttons(request.request_id, request.version, language)))


def service_functions(inputs: ReasonInputs) -> tuple[ServiceFunction, ...]:
    service = AccessService(inputs)
    return (ServiceFunction("access", (), CALLBACK_PREFIX, "owner", service.on_button,
                            text_input=TextInput(inputs.waiting, service.on_reason, inputs.cancel)),)


SERVICE_FUNCTIONS = service_functions(ReasonInputs())
