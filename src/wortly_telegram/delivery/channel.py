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
the outcome and the exception class only, never the text, the chat or Telegram's answer.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
import logging
from typing import Any, Protocol

from telegram import Bot

from wortly.jobs.channel import SENT, ChannelResult
from wortly.jobs.models import DeliverySnapshot

from ..messages.registry import TemplateRegistry
from ..webapp import keyboard
from .classify import PRE_SEND_ERROR, classify


logger = logging.getLogger(__name__)


class PausableCapacity(Protocol):
    def pause(self, duration: Any) -> None: ...


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
        try:
            prepared = self._templates.prepare(snapshot.context.kind, snapshot.facts.facts_json)
            markup = keyboard(prepared.message, self._web_app_url, prepared.language)
        except Exception as error:  # noqa: BLE001 - every fault here precedes the network
            logger.warning("delivery not formatted: %s", type(error).__name__)
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
