"""The core replaced in tests (design T-D §9.5 а, answer 2.4).

FakeCoreGateway replaces the whole gateway in handler tests and records which principal
each operation received, so a test can prove the subject came from
``update.effective_user.id`` and nowhere else. FakeCore replaces only the core's
internal API under the real gateway (httpx.MockTransport).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
import json
import socket
import time
from typing import Any

import httpx

from wortly.identity.principal import TelegramPrincipal
from wortly_telegram.config import CoreSettings
from wortly_telegram.gateway import CoreError, PendingRequest


# The core's bound of a rejection reason, in code points (identity/service.py, C-ADAPTER-3).
REASON_MAX_CHARACTERS = 500


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
        # An error raised by one operation only, e.g. {"decide": CoreError(...)}.
        self.errors: dict[str, Exception] = {}
        self.result: Any = None
        # Principals whose "bot blocked" mark the core still holds; a contact lifts it.
        self.blocked: set[str] = set()
        # What pending_requests answers, oldest first.
        self.pending: list[PendingRequest] = []

    def _record(self, operation: str, principal: TelegramPrincipal, **arguments: Any) -> None:
        if not isinstance(principal, TelegramPrincipal):
            raise TypeError("The gateway takes a TelegramPrincipal only")
        self.calls.append(GatewayCall(operation, principal, arguments))
        if self.error is not None:
            raise self.error
        if operation in self.errors:
            raise self.errors[operation]

    async def profile_summary(self, principal: TelegramPrincipal) -> dict | None:
        self._record("profile_summary", principal)
        return self.summaries.get(principal.external_id)

    async def note_contact(self, principal: TelegramPrincipal) -> None:
        self._record("note_contact", principal)
        self.blocked.discard(principal.external_id)

    async def decide(self, principal: TelegramPrincipal, **arguments: Any) -> Any:
        self._record("decide", principal, **arguments)
        reason = arguments.get("reason")
        # The core's own rules for a reason (C-ADAPTER-3): only with a rejection, at most
        # REASON_MAX_CHARACTERS code points; otherwise 422 VALIDATION_FAILED.
        if reason is not None and (arguments.get("approve") or len(reason) > REASON_MAX_CHARACTERS):
            raise CoreError("VALIDATION_FAILED", status=422)
        return self.result

    async def pending_requests(self, principal: TelegramPrincipal) -> tuple[PendingRequest, ...]:
        self._record("pending_requests", principal)
        return tuple(self.pending)

    async def change_mode(self, principal: TelegramPrincipal, **arguments: Any) -> Any:
        self._record("change_mode", principal, **arguments)
        return self.result


# -- a core builder's facts without a database (template contract tests) ----------------


class ScriptedFacts:
    """A FactsReader of the core's builders that answers scripted rows instead of SQL.

    Each rule pairs a fragment of the builder's query with the rows it returns; the first
    rule whose fragment the query contains answers it. A query no rule names fails the
    test, so a builder that starts reading something new is noticed. The template tests
    run the core's real builder over it and render the facts it returns.
    """

    def __init__(self, *rules: tuple[str, list[dict]]) -> None:
        self.rules = rules
        self.queries: list[tuple[str, tuple]] = []

    def fetch_all(self, query: str, parameters=()) -> list[dict]:
        self.queries.append((query, tuple(parameters)))
        for fragment, rows in self.rules:
            if fragment in query:
                return [dict(row) for row in rows]
        raise AssertionError(f"No scripted rows for the query: {query}")

    def fetch_one(self, query: str, parameters=()) -> dict | None:
        rows = self.fetch_all(query, parameters)
        if len(rows) > 1:
            raise AssertionError("Expected at most one fact row")
        return rows[0] if rows else None


# The fragments of the shared reads of every builder (core jobs/builders/_recipient.py).
RECIPIENT_QUERY = "FROM profiles p LEFT JOIN profile_settings"
JOB_EVENT_QUERY = "FROM jobs WHERE id=%s"


def recipient_row(*, language: str = "ru", telegram_user_id: str = "4242", role: str = "owner",
                  status: str = "active", blocked: bool = False) -> dict:
    return {"status": status, "role": role, "access_version": 3, "ui_language": language,
            "settings_version": 5, "external_id": telegram_user_id, "blocked": blocked}


# -- the core's internal API behind httpx.MockTransport (answer 2.4) ----------------------

# Fictitious values: the service secret of the tests and the core's address.
CORE_SECRET = "synthetic-service-secret-FAKE"
CORE_URL = "http://core.internal.test:8001"
SECRET_HEADER = "x-wortly-service-secret"


def core_settings(**overrides) -> CoreSettings:
    values = dict(internal_url=CORE_URL, service_secret=CORE_SECRET, connect_timeout=timedelta(seconds=1),
                  read_timeout=timedelta(seconds=2))
    values.update(overrides)
    return CoreSettings(**values)


def envelope(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code}})


@dataclass
class FakeLease:
    holder: str
    generation: int
    # When the fake core lets it expire, on the clock the fake core was given.
    expires_at: float


class FakeCore:
    """The core's internal API as the bot sees it: one MockTransport, no socket.

    Each request is recorded. A scripted answer per path (a response, an exception the
    transport raises, or an async callable of the request) is used first; without one
    the fake answers like the core: the secret gate (one exact header, else 401), an
    empty profile summary, contact 204, the requests of ``pending``, the owner commands
    applied, and the "bot" lease kept in memory with the core's rules (one holder, a new generation per
    acquisition, renew and release by holder and generation). ``now`` is the fake
    core's clock and ``delay`` an awaitable the lease routes wait before answering (a
    slow core); the lease is computed on arrival, as the core computes it at the start
    of its transaction.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic,
                 delay: Callable[[str], Awaitable[None]] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.script: dict[str, deque] = defaultdict(deque)
        self.now = now
        self.delay = delay
        self.lease: FakeLease | None = None
        self.generations = 0
        self.cancelled: list[str] = []
        # The pending_requests answer, in the core's wire shape (PendingRequestsResponse).
        self.pending: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def answer(self, path: str, *answers) -> None:
        self.script[path].extend(answers)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(request.content) for request in self.requests if request.url.path == path]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if self.script[path]:
            answer = self.script[path].popleft()
            if isinstance(answer, BaseException):
                raise answer
            if callable(answer):
                return await answer(request)
            return answer
        if request.headers.get_list(SECRET_HEADER) != [CORE_SECRET]:
            return envelope(401, "AUTH_INVALID")
        body = json.loads(request.content) if request.content else {}
        if path.startswith("/internal/v1/leases/bot/"):
            try:
                return await self._lease_route(path.rsplit("/", 1)[1], body)
            except asyncio.CancelledError:
                self.cancelled.append(path)
                raise
        if path == "/internal/v1/admission/summary":
            return httpx.Response(200, json={"profile": None})
        if path == "/internal/v1/contact":
            return httpx.Response(204)
        if path == "/internal/v1/admission/pending_requests":
            return httpx.Response(200, json={"requests": list(self.pending)})
        if path in ("/internal/v1/admission/decide", "/internal/v1/admission/change_mode"):
            command = {"id": body["command_id"], "status": "applied", "applied_at": "2026-10-06T12:00:00Z",
                       "target_id": None, "object_version": "2"}
            return httpx.Response(200, json={"command": command, "current": {"mode": "manual", "version": "2"}})
        return envelope(404, "NOT_FOUND")

    async def _lease_route(self, action: str, body: dict) -> httpx.Response:
        arrived = self.now()
        lease = self.lease
        if action == "acquire":
            if lease is not None and lease.expires_at > arrived:
                answer = envelope(409, "LEASE_HELD")
            else:
                self.generations += 1
                self.lease = lease = FakeLease(body["holder"], self.generations, arrived + body["duration_seconds"])
                answer = self._granted(lease)
        else:
            held = (lease is not None and lease.holder == body["holder"]
                    and str(lease.generation) == body["generation"] and lease.expires_at > arrived)
            if action == "renew":
                if held:
                    lease.expires_at = arrived + body["duration_seconds"]
                answer = self._granted(lease) if held else envelope(409, "LEASE_LOST")
            else:
                if held:
                    lease.expires_at = arrived
                answer = httpx.Response(200, json={"released": held})
        if self.delay is not None:
            await self.delay(action)
        return answer

    @staticmethod
    def _granted(lease: FakeLease) -> httpx.Response:
        return httpx.Response(200, json={"role": "bot", "holder": lease.holder, "generation": str(lease.generation),
                                         "expires_at": "2026-10-06T12:01:00Z"})


def forbid_network(monkeypatch) -> list:
    """Make every IP socket connect and name lookup fail; returns the attempts (T1-QA)."""
    attempts: list = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refused(*args, **kwargs):
        attempts.append(args[1:] or kwargs)
        raise OSError("network is forbidden in the tests")

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            return refused(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            return refused(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    return attempts
