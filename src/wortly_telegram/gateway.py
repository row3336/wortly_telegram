"""The bot's one door to the core: the core's internal HTTP API (fork R1 resolved: variant B, answer 2.4).

The bot process holds no database credentials. Every core operation is a request to the
core's internal listener (core: wortly.api.internal, docs/openapi_internal.json):

- The service secret travels only in the ``X-Wortly-Service-Secret`` header of each
  request. It is never a default header of the client, never part of a URL, a repr or a
  log line, and the bot process adds it to its log redaction (bot_main).
- Redirects are never followed and paths never end with "/": a near-miss path must not
  carry the secret to another URL (C-ADAPTER-2-QA). The client ignores proxy variables of
  the environment (trust_env=False) for the same reason.
- Each call has one deadline, connect plus read timeout of [core] (config.py), however
  slowly the core trickles its answer. config.py checks that the deadline is shorter than
  the leader's stop margin.
- No call is ever repeated here. A command keeps the command_id the router derived from
  the update (ВП-6), so a user's repeat reaches the core as the same command.
- The core's refusals (401, 403, 409, 422 with the error envelope) become CoreError
  with the core's code and status, the attributes of the IdentityError the core's
  AdmissionAdapter raised in process. Anything else, no answer, a timeout, a 5xx, a
  redirect, an answer outside the contract, is CoreUnavailable (a CoreError with status
  503): the bot cannot tell whether a command was applied, and the same command_id makes
  a repeat safe. The router and the start handler catch any exception of the gateway.

Subject (§1.2, QA C-ADAPTER): ``principal_of`` builds the TelegramPrincipal from
``update.effective_user.id`` of an update this bot's poller received, never from text,
callback data, initData or configuration. The core builds its own principal from
``actor_telegram_id`` behind the secret; a role in a profile summary grants nothing.

The bot poller's lease (§2.4.1) goes over the same client: HttpLeaseStore keeps the
pair (holder, generation) the core answered and counts nothing itself; the leader
(leader.py) counts the lease on its monotonic clock from before the request was sent.

Trail (R3-03, events TZ §4.4): every call carries ``X-Wortly-Trace-Id`` and writes one
``b.core`` line (trace, route template, status, the core's own error code, milliseconds; for
a call that got no answer the exception class instead of a status). The trace is the one of
the update being handled, or a fresh one outside an update (trail.call_trace). The line
never holds the body, the actor, the query or a path with a value: the routes of this client
are fixed templates and every identifier travels in the body. The poller's lease calls write
a line only when they fail: renewals come every third of a lease and a standby bot's
refused acquire (409 LEASE_HELD) every standby_retry; neither is a user path.

Nothing here imports a database driver or a core service (design §9.5, finding Н2).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import re
import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx
from telegram import Update

from wortly.identity.principal import TelegramPrincipal

from .leader import ROLE
from .trail import BOT, TRACE_HEADER, call_trace, emit

if TYPE_CHECKING:
    from .config import CoreSettings


# The core's names (wortly.api.internal); the integration test checks they still match.
SERVICE_SECRET_HEADER = "X-Wortly-Service-Secret"
PREFIX = "/internal/v1"
UNAVAILABLE = "WRITE_UNAVAILABLE"

# An error code of the envelope: a constant of the core, never free text.
_CODE = re.compile(r"[A-Z][A-Z_]{0,63}")
_VERSION = re.compile(r"[1-9][0-9]{0,18}")
_REFUSALS = frozenset((400, 401, 403, 404, 405, 409, 413, 422))
_ROLES = frozenset(("owner", "user"))
_STATUSES = frozenset(("admitted", "pending", "rejected", "deleted"))
_LANGUAGES = frozenset(("uk", "en", "ru", None))


def principal_of(update: Update) -> TelegramPrincipal:
    """The sender of an update received by this bot's own poller."""
    user = update.effective_user
    if user is None:
        raise ValueError("The update has no sender")
    return TelegramPrincipal(str(user.id))


class CoreError(Exception):
    """A refusal of the core: its envelope code (a constant, never data) and HTTP status."""

    def __init__(self, code: str, *, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class CoreUnavailable(CoreError):
    """The core gave no usable answer: unreachable, too slow, failing or outside the contract."""

    def __init__(self) -> None:
        super().__init__(UNAVAILABLE, status=503)


@dataclass(frozen=True)
class HeldLease:
    """The bot lease as the core granted it; renew and release send holder and generation."""

    holder: str
    generation: str
    # Database time, diagnostics only: the leader never compares it with its own clock.
    expires_at: str = field(compare=False)


@dataclass(frozen=True)
class PendingRequest:
    """One access request the owner can still decide (core: PendingRequest, C-ADAPTER-3).

    ``version`` is the expected_version of a decision on it. The applicant's Telegram ID,
    first name and username are personal data and stay out of the repr (TC:273); any of
    them may be None, and the username comes without "@".
    """

    request_id: UUID
    version: int
    telegram_user_id: str | None = field(repr=False)
    first_name: str | None = field(repr=False)
    username: str | None = field(repr=False)
    requested_at: str = field(repr=False)


def is_version(value: Any) -> bool:
    """Whether ``value`` is a version of the core on the wire: a positive bigint as a string."""
    return type(value) is str and _VERSION.fullmatch(value) is not None


def is_optional_text(value: Any) -> bool:
    """Whether ``value`` is a text field the core may leave empty: a string or None."""
    return value is None or type(value) is str


def _optional_text(value: Any) -> str | None:
    if not is_optional_text(value):
        raise CoreUnavailable()
    return value


def _pending_request(value: Any) -> PendingRequest:
    item = _object(value, {"request_id", "request_version", "applicant", "requested_at"})
    applicant = _object(item["applicant"], {"telegram_user_id", "first_name", "username"})
    version, requested_at = item["request_version"], item["requested_at"]
    if (not is_version(version) or type(requested_at) is not str
            or type(item["request_id"]) is not str):
        raise CoreUnavailable()
    try:
        request_id = UUID(item["request_id"])
    except ValueError:
        raise CoreUnavailable() from None
    return PendingRequest(request_id, int(version), _optional_text(applicant["telegram_user_id"]),
                          _optional_text(applicant["first_name"]), _optional_text(applicant["username"]),
                          requested_at)


def _refusal(response: httpx.Response) -> CoreError:
    """The core's refusal with its own code, or CoreUnavailable when there is none."""
    if response.status_code not in _REFUSALS:
        return CoreUnavailable()
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        # A refusal without the envelope came from something else than the core (a proxy).
        return CoreUnavailable()
    if type(code) is not str or not _CODE.fullmatch(code):
        return CoreUnavailable()
    return CoreError(code, status=response.status_code)


def _object(value: Any, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise CoreUnavailable()
    return value


def _wire_code(response: httpx.Response) -> str | None:
    """The error code the core's envelope carries, or None; never any other part of the answer."""
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if type(code) is str and _CODE.fullmatch(code) else None


def _version(value: int) -> str:
    # Versions are bigint strings on the wire; the router counts them as integers.
    if type(value) is not int or value < 1:
        raise ValueError("A version is a positive integer")
    return str(value)


def _actor(principal: TelegramPrincipal) -> dict:
    if not isinstance(principal, TelegramPrincipal):
        raise TypeError("The core is called for a TelegramPrincipal only")
    return {"actor_telegram_id": principal.external_id}


class InternalClient:
    """One httpx client to the core's internal listener, shared by the gateway and the lease store."""

    def __init__(self, core: CoreSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = core.internal_url + PREFIX
        self._secret = core.service_secret
        self._deadline = core.request_budget.total_seconds()
        connect, read = core.connect_timeout.total_seconds(), core.read_timeout.total_seconds()
        self._client = httpx.AsyncClient(
            transport=transport, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(connect=connect, read=read, write=read, pool=connect))

    def __repr__(self) -> str:
        return f"InternalClient({self._base!r})"

    async def call(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        """One request within the deadline; CoreUnavailable when no answer came."""
        trace, started = call_trace(), time.monotonic()
        try:
            async with asyncio.timeout(self._deadline):
                response = await self._client.request(
                    method, self._base + path, json=body,
                    headers={SERVICE_SECRET_HEADER: self._secret, TRACE_HEADER: trace})
        except (httpx.HTTPError, TimeoutError, OSError) as error:
            self._trail(path, trace, started, err=type(error).__name__)
            # from None: a transport error's text may name the address; the class says enough.
            raise CoreUnavailable() from None
        self._trail(path, trace, started, status=response.status_code,
                    code=None if response.is_success else _wire_code(response))
        return response

    def _trail(self, path: str, trace: str, started: float, *, status: int | None = None,
               code: str | None = None, err: str | None = None) -> None:
        """The ``b.core`` line of one call; the lease's successful and held-by-another calls are not a user path."""
        if path.startswith("/leases/") and (status is not None and 200 <= status < 300
                                             or (status, code) == (409, "LEASE_HELD")):
            return
        ms = max(0, round((time.monotonic() - started) * 1000))
        emit("b.core", component=BOT, trace=trace, route=PREFIX + path, status=status, code=code, ms=ms,
             err=err, lvl="warning" if status is None or status >= 500 else None)

    async def answer(self, path: str, body: dict, *, status: int = 200) -> Any:
        """POST ``body``; the JSON answer of ``status``, or the core's refusal raised."""
        response = await self.call("POST", path, body)
        if response.status_code != status:
            raise _refusal(response)
        if status == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise CoreUnavailable() from None

    async def aclose(self) -> None:
        await self._client.aclose()


class HttpCoreGateway:
    """Owner operations, the profile summary and the contact note over the internal API."""

    def __init__(self, client: InternalClient) -> None:
        self.client = client
        self.leases = HttpLeaseStore(client)

    async def profile_summary(self, principal: TelegramPrincipal) -> dict | None:
        """Role, status ("admitted" when active) and interface language; None without a profile."""
        answer = _object(await self.client.answer("/admission/summary", _actor(principal)), {"profile"})
        profile = answer["profile"]
        if profile is None:
            return None
        _object(profile, {"role", "status", "ui_language"})
        if (profile["role"] not in _ROLES or profile["status"] not in _STATUSES
                or profile["ui_language"] not in _LANGUAGES):
            raise CoreUnavailable()
        return dict(profile)

    async def note_contact(self, principal: TelegramPrincipal, *, age_ms: int = 0) -> None:
        """The principal wrote to the bot or unblocked it: lift its "bot blocked" mark, if any.

        Answer 2.5, Я18. Idempotent and needs no admission. The router gives it
        CONTACT_TIMEOUT and never lets its failure keep the user from an answer.
        ``age_ms`` tells the core how long ago the contact happened when the router can
        note it only by a later retry (PP31, finding 1.8); a live contact sends none.
        """
        if type(age_ms) is not int or age_ms < 0:
            raise ValueError("A contact's age is a non-negative number of milliseconds")
        body = _actor(principal) | ({"age_ms": age_ms} if age_ms else {})
        await self.client.answer("/contact", body, status=204)

    async def decide(self, principal: TelegramPrincipal, *, command_id: UUID, request_id: UUID,
                     expected_version: int, approve: bool, reason: str | None) -> dict:
        """The owner's decision on an access request; the core's {"command", "current"} answer."""
        if type(approve) is not bool:
            raise TypeError("approve is a boolean")
        body = {**_actor(principal), "command_id": str(UUID(str(command_id))),
                "request_id": str(UUID(str(request_id))), "expected_version": _version(expected_version),
                "approve": approve}
        if reason is not None:
            body["reason"] = reason
        return _object(await self.client.answer("/admission/decide", body), {"command", "current"})

    async def pending_requests(self, principal: TelegramPrincipal) -> tuple[PendingRequest, ...]:
        """The requests waiting for a decision, oldest first; FORBIDDEN unless an admitted owner."""
        answer = _object(await self.client.answer("/admission/pending_requests", _actor(principal)), {"requests"})
        if not isinstance(answer["requests"], list):
            raise CoreUnavailable()
        return tuple(_pending_request(item) for item in answer["requests"])

    async def change_mode(self, principal: TelegramPrincipal, *, command_id: UUID, expected_version: int,
                          mode: str) -> dict:
        """The owner's admission mode change; the core's {"command", "current"} answer."""
        body = {**_actor(principal), "command_id": str(UUID(str(command_id))),
                "expected_version": _version(expected_version), "mode": mode}
        return _object(await self.client.answer("/admission/change_mode", body), {"command", "current"})

    async def aclose(self) -> None:
        await self.client.aclose()


class HttpLeaseStore:
    """The bot poller's lease (leader.LeaseStore) over the core's internal lease routes."""

    def __init__(self, client: InternalClient) -> None:
        self._client = client

    @staticmethod
    def _seconds(duration: timedelta) -> int:
        # The route takes whole seconds >= 1; config.py reads the lease as whole seconds.
        if type(duration) is not timedelta or duration < timedelta(seconds=1) or duration % timedelta(seconds=1):
            raise ValueError("A lease duration is a whole number of seconds, at least one")
        return int(duration.total_seconds())

    @staticmethod
    def _lease(answer: Any, holder: str) -> HeldLease:
        lease = _object(answer, {"role", "holder", "generation", "expires_at"})
        generation = lease["generation"]
        if (lease["role"] != ROLE or lease["holder"] != holder or not is_version(generation)
                or type(lease["expires_at"]) is not str):
            raise CoreUnavailable()
        return HeldLease(holder, generation, lease["expires_at"])

    async def acquire(self, role: str, *, holder: str, duration: timedelta) -> HeldLease | None:
        """Take the free or expired lease; None while another holder has it (LEASE_HELD)."""
        if role != ROLE:
            raise ValueError("Only the bot lease is served to the bot")
        body = {"holder": holder, "duration_seconds": self._seconds(duration)}
        try:
            answer = await self._client.answer(f"/leases/{ROLE}/acquire", body)
        except CoreError as error:
            if (error.status, error.code) == (409, "LEASE_HELD"):
                return None
            raise
        return self._lease(answer, holder)

    async def renew(self, lease: HeldLease, *, duration: timedelta) -> HeldLease:
        """Extend the held lease; raises (LEASE_LOST or anything else) once it cannot be kept."""
        body = {"holder": lease.holder, "generation": lease.generation,
                "duration_seconds": self._seconds(duration)}
        renewed = self._lease(await self._client.answer(f"/leases/{ROLE}/renew", body), lease.holder)
        if renewed != lease:
            # A renewal keeps the generation; another one means the core lost track of ours.
            raise CoreUnavailable()
        return renewed

    async def release(self, lease: HeldLease) -> bool:
        """Give the lease back now; False when it had already expired or changed hands."""
        answer = _object(await self._client.answer(
            f"/leases/{ROLE}/release", {"holder": lease.holder, "generation": lease.generation}), {"released"})
        if type(answer["released"]) is not bool:
            raise CoreUnavailable()
        return answer["released"]


def build_gateway(core: CoreSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> HttpCoreGateway:
    """The gateway (and its lease store) over one client to the core's internal API."""
    return HttpCoreGateway(InternalClient(core, transport=transport))
