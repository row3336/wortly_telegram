"""The core gateway replaced in handler tests (design T-D §9.5 а).

It records which principal each operation received, so a test can prove the subject
came from ``update.effective_user.id`` and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wortly.identity.principal import TelegramPrincipal


@dataclass(frozen=True)
class GatewayCall:
    operation: str
    principal: TelegramPrincipal
    arguments: dict[str, Any] = field(default_factory=dict)


class FakeCoreGateway:
    def __init__(self, summaries: dict[str, dict | None] | None = None) -> None:
        self.summaries = dict(summaries or {})
        self.calls: list[GatewayCall] = []
        self.error: Exception | None = None
        self.result: Any = None
        # Principals whose "bot blocked" mark the core still holds; a contact lifts it.
        self.blocked: set[str] = set()

    def _record(self, operation: str, principal: TelegramPrincipal, **arguments: Any) -> None:
        if not isinstance(principal, TelegramPrincipal):
            raise TypeError("The gateway takes a TelegramPrincipal only")
        self.calls.append(GatewayCall(operation, principal, arguments))
        if self.error is not None:
            raise self.error

    async def profile_summary(self, principal: TelegramPrincipal) -> dict | None:
        self._record("profile_summary", principal)
        return self.summaries.get(principal.external_id)

    async def note_contact(self, principal: TelegramPrincipal) -> None:
        self._record("note_contact", principal)
        self.blocked.discard(principal.external_id)

    async def decide(self, principal: TelegramPrincipal, **arguments: Any) -> Any:
        self._record("decide", principal, **arguments)
        return self.result

    async def change_mode(self, principal: TelegramPrincipal, **arguments: Any) -> Any:
        self._record("change_mode", principal, **arguments)
        return self.result
