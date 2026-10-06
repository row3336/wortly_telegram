"""The bot's one door to the core: fork Р1, working variant A until the owner answers Q28.

Variant A (design T-D §1.1): the bot calls the core's application layer in its own
process. The core's services are synchronous and open and commit their own
transaction, so each call runs in a worker thread (asyncio.to_thread) and the bot
answers Telegram only after it returned: no Telegram call can happen inside a core
transaction (§3.6).

The services are imported inside ``build_gateway``, never at module level: handlers
import this module and must load without psycopg (finding Н2, §9.5). If Q28 is
answered "only through the API", this module alone changes: CoreGateway becomes a
client of the core's internal routes with the same four methods.

Subject (§1.2, QA C-ADAPTER): ``principal_of`` builds the TelegramPrincipal from
``update.effective_user.id`` of an update this bot's poller received, never from
text, callback data, initData or configuration. The bot never builds a
TelegramIdentity and never calls login(); a role in a profile summary grants nothing
by itself, the core checks every right again under its locks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from telegram import Update

from wortly.identity.principal import TelegramPrincipal


def principal_of(update: Update) -> TelegramPrincipal:
    """The sender of an update received by this bot's own poller."""
    user = update.effective_user
    if user is None:
        raise ValueError("The update has no sender")
    return TelegramPrincipal(str(user.id))


class CoreGateway:
    """Owner operations, the profile summary and the contact note of the core's AdmissionAdapter."""

    def __init__(self, admission: Any, *,
                 call: Callable[..., Awaitable[Any]] = asyncio.to_thread) -> None:
        self._admission = admission
        self._call = call

    async def profile_summary(self, principal: TelegramPrincipal) -> dict | None:
        """Role, status ("admitted" when active) and interface language; None without a profile."""
        return await self._call(self._admission.profile_summary, principal)

    async def note_contact(self, principal: TelegramPrincipal) -> None:
        """The principal wrote to the bot: lift its "bot blocked" mark, if any (answer 2.5).

        Idempotent and needs no admission. T-GW-HTTP moves this call to the core's internal
        route together with the other methods; the bot's behaviour does not depend on it.
        """
        await self._call(self._admission.note_contact, principal)

    async def decide(self, principal: TelegramPrincipal, *, command_id: UUID, request_id: UUID,
                     expected_version: int, approve: bool, reason: str | None) -> Any:
        return await self._call(self._admission.decide_as, principal, command_id=command_id,
                                request_id=request_id, expected_version=expected_version,
                                approve=approve, reason=reason)

    async def change_mode(self, principal: TelegramPrincipal, *, command_id: UUID, expected_version: int,
                          mode: str) -> Any:
        return await self._call(self._admission.change_mode_as, principal, command_id=command_id,
                                expected_version=expected_version, mode=mode)


def build_gateway(connections: Callable, *, clock: Callable[[], datetime],
                  session_lifetime: timedelta) -> CoreGateway:
    """The gateway over the bot's own writable-leader connections (variant A)."""
    from wortly.identity.admission_adapter import AdmissionAdapter
    from wortly.identity.service import IdentityService

    service = IdentityService(connections, clock=clock, session_lifetime=session_lifetime)
    return CoreGateway(AdmissionAdapter(service))
