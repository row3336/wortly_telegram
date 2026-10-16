"""The Telegram channel of the core's delivery loop (design T-D §3.1, §3.4, §4.1).

``deliver`` gets a snapshot whose ``sending`` attempt is already committed. It renders
the facts with the job kind's template and then makes exactly one Bot API call,
sendMessage. It never repeats that call, never waits for capacity or retry_after, and
always answers a ChannelResult with the class of its proof (QA C-JOBS): the core's
retry policy decides whether and when another attempt follows (TC:282).

- A fault before the network (template, language, recipient, Mini App address) is
  ``pre_send_error`` and no request is made (§3.3, Д4).
- A 429 with retry_after also pauses this process's capacity gate, so the next claim
  waits for it (§3.5 п.2).
- An InvalidToken marks the channel as misconfigured: the process is no longer ready
  and takes no further job (§3.3, §2.3).

The coroutine runs on the event loop that owns the telegram.Bot; ``run`` hands it over
from the delivery thread and waits for the result (delivery.main, ВП-13). Logs name
the outcome and the exception class only, never the text, the chat or Telegram's answer; a fault of
the template step adds the category of its RenderError (a word of messages.REASONS), so an unsupported
envelope schema is told from an unknown kind or a body of another form.

Trail (R3-03, events TZ §4.3): every attempt that reaches the channel writes one ``d.job.done``
line with the job, the attempt (its ID and number), the kind, the outcome, the failure class
and the milliseconds spent here. The outcome is ``sent``; ``unknown``; ``retry`` for a proven
nondelivery the core's retry policy will try again; ``failed`` (a warning) for one it closes
(core retry_policy.RETRY_RULES, the same table ``resolve`` uses). A kept-back one-off job of a
confirmed block counts as ``retry``: ``resolve`` fails it only when its 24 hours are over, which the
channel does not know. The line is written when the channel answers, before ``resolve`` commits.
It names no recipient: a snapshot reaches this channel only after the kind's builder found the
recipient active (every builder of the core closes the job otherwise, builders/_recipient.py),
so the ``job`` of a line never belongs to a profile that is not admitted (root decision P3-4).
Never the chat, the text, the facts, ``logical_key`` or the snapshot.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
import logging
import time
from typing import Any, Protocol

from telegram import Bot

from wortly.jobs.channel import SENT, ChannelResult
from wortly.jobs.models import DeliverySnapshot
from wortly.jobs.retry_policy import KIND_CLASSES, RETRY_RULES

from ..messages import RenderError
from ..messages.registry import TemplateRegistry
from ..trail import DELIVERY, emit
from ..webapp import keyboard
from .classify import PRE_SEND_ERROR, classify


logger = logging.getLogger(__name__)


def attempt_outcome(kind: str, result: ChannelResult) -> str:
    """The trail's outcome of an attempt: sent, unknown, retry or failed (see the module text)."""
    if result.outcome == "sent":
        return "sent"
    if result.outcome == "unknown":
        return "unknown"
    klass = KIND_CLASSES.get(kind)
    if result.reason is None or klass is None:
        # A legacy class-less result is repeated at once; a kind without a rule never reaches here.
        return "retry"
    return "failed" if RETRY_RULES[klass][result.reason] == "close" else "retry"


class PausableCapacity(Protocol):
    def pause(self, duration: Any) -> None: ...


def _fault_reason(error: Exception) -> str:
    """The category of a RenderError, one word of messages.REASONS (never a text or a fact); "-" for any other fault."""
    return error.reason if isinstance(error, RenderError) else "-"


class TelegramChannel:
    def __init__(self, bot: Bot, *, templates: TemplateRegistry, web_app_url: str,
                 capacity: PausableCapacity, run: Callable[[Coroutine], Any]) -> None:
        self._bot = bot
        self._templates = templates
        self._web_app_url = web_app_url
        self._capacity = capacity
        self._run = run
        self._token_rejected = False

    @property
    def token_rejected(self) -> bool:
        """True once Telegram refused the token: the process must take no further job."""
        return self._token_rejected

    def deliver(self, snapshot: DeliverySnapshot) -> ChannelResult:
        started = time.monotonic()
        result = self._deliver(snapshot)
        try:
            kind = snapshot.context.kind
            outcome = attempt_outcome(kind, result)
            emit("d.job.done", component=DELIVERY, job=snapshot.claim.job_id, att=snapshot.attempt_id,
                 kind=kind, outcome=outcome, reason=result.reason, attempt=snapshot.attempt,
                 ms=max(0, round((time.monotonic() - started) * 1000)),
                 lvl="warning" if outcome == "failed" else None)
        except Exception:  # noqa: BLE001 - an exception here would turn a known result into "unknown"
            pass
        return result

    def _deliver(self, snapshot: DeliverySnapshot) -> ChannelResult:
        try:
            prepared = self._templates.prepare(snapshot.context.kind, snapshot.facts.facts_json)
            markup = keyboard(prepared.message, self._web_app_url, prepared.language)
        except Exception as error:  # noqa: BLE001 - every fault here precedes the network
            # A RenderError adds its category (schema_mismatch, unknown_kind, body_keys, ...), so an unsupported
            # envelope schema is told from a body of another form; never its text. Any other fault: the type only.
            logger.warning("delivery not formatted: %s: %s", type(error).__name__, _fault_reason(error))
            return PRE_SEND_ERROR
        # Only a quiet message names the option; every other call stays as it was.
        options: dict[str, Any] = {"disable_notification": True} if prepared.message.silent else {}
        if prepared.message.parse_mode is not None:  # among the jobs the #daily block only (R9-04)
            options["parse_mode"] = prepared.message.parse_mode
        try:
            self._run(self._bot.send_message(chat_id=prepared.chat_id, text=prepared.message.text,
                                             reply_markup=markup, **options))
        except Exception as error:  # noqa: BLE001 - classified, never raised to the loop
            result = classify(error)
            if result.reason == "rate_limited":
                self._capacity.pause(result.retry_after)
            elif result.reason == "channel_config":
                self._token_rejected = True
            logger.warning("delivery %s %s: %s", result.outcome, result.reason or "-", type(error).__name__)
            return result
        return SENT
