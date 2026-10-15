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

The owner's common editor (project DEV-17 §4.2, stage B1a; core C1-C3): ``owner_search``,
``owner_card``, ``translation_op`` and ``grammar_save`` post to the core's ``/owner/cards/*``
routes. Each answer is checked against the internal schema (docs/openapi_internal.json:
OwnerSearchResponse, OwnerCardResponse, OwnerCommandResponse) field by field, and an answer
outside it is CoreUnavailable, as for every other route. A command's body is built only from
the arguments, never from an earlier answer, so the same update gives the same body (§4.4).

Synonyms (stage B1b, core C4): ``synonym_candidates`` and the commands ``synonym_link``,
``synonym_link_many``, ``synonym_unlink`` and ``synonym_clear`` post to ``/owner/synonyms/*``.
The decoder knows the C4 schema exactly: OwnerCard carries ``synonyms`` (SynonymList) and
OwnerSearchCard ``synonym_group_version``; an answer without them (a core before C4) is
outside the contract like any other, so the bot and C4 are released together (gate C334).

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


# -- the owner's common editor (project DEV-17 §4.2; core internal_models.Owner*) ---------

# The closed lists of the internal schema (core api/internal_models.py, dictionary/validation.py).
OWNER_LANGUAGES = ("ru", "uk", "en")
CARD_TYPES = ("noun", "verb", "phrase", "adjective", "pronoun", "adverb", "preposition", "conjunction",
              "particle")
CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2", None)
CARD_STATES = ("visible", "no_translation")
FORM_STATUSES = ("applicable", "not_applicable", "unknown")
SEPARABILITY = ("separable", "inseparable", "unknown")
TRANSLATION_OPERATIONS = ("replace", "append", "remove", "move_up", "move_down")
COMMAND_STATUSES = ("applied", "already_applied")
# The core's bounds (dictionary/owner_editor.py): the index of a list operation, the query, the limit.
INDEX_MAX = 99
QUERY_MAX_CHARACTERS = 200
SEARCH_LIMIT_MAX = 10
# The core's bounds of synonyms (dictionary/synonyms.py): cards of one link_many, candidates offered.
LINK_MANY_MAX = 20
CANDIDATES_MAX = 20
# The grammar a card type carries on the wire (ReferenceNoun, ReferenceVerb, ReferenceAdjective):
# a form field with its own status, its closed values (None: free text), and the status-only and
# plain-value fields.
_FORM_FIELDS: dict[str, dict[str, tuple[str, ...] | None]] = {
    "noun": {"gender": ("masculine", "feminine", "neuter"), "singular": None, "plural": None},
    "verb": {"infinitive": None, "preterite": None, "participle_ii": None, "auxiliary": ("haben", "sein")},
    "adjective": {"positive": None, "comparative": None, "superlative": None},
}
_STATUS_ONLY = {"noun": ("article",)}
_PLAIN = {"verb": ("separability",)}


@dataclass(frozen=True)
class GrammarForm:
    """One grammar column of a card: its applicability and at most one form (core validation.Form)."""

    status: str
    value: str | None = None


@dataclass(frozen=True)
class SynonymMember:
    """One published card of a synonym group as the owner sees it (core SynonymMember)."""

    card_id: UUID
    lemma: str = field(repr=False)
    card_type: str
    cefr: str | None
    state: str
    preview: str | None = field(repr=False)


@dataclass(frozen=True)
class SynonymList:
    """The synonym group of a card: ``version`` is the expected_group_version of the next command.

    None without a group, then ``members`` is empty. Members by card id, the card itself
    included; ``index`` of an unlink counts from 1 in this list.
    """

    version: int | None
    members: tuple[SynonymMember, ...]


@dataclass(frozen=True)
class SynonymGroup:
    """The group of ``card_id`` in the answer of a synonym command (core SynonymGroup)."""

    card_id: UUID
    card_type: str
    synonyms: SynonymList


@dataclass(frozen=True)
class SynonymCommand:
    """The core's answer to a synonym command; ``current`` is None for a demoted actor's repeat."""

    status: str
    version: int
    current: SynonymGroup | None


@dataclass(frozen=True)
class OwnerCard:
    """A published card as the owner edits it in the bot (core OwnerCard).

    ``revision`` versions every common field (card_revision). ``translations`` holds every
    language in display order, an untranslated one empty. ``grammar`` maps each editable
    field of the type to a GrammarForm, ``separability`` to its plain value; None for a type
    without grammar or a card without its grammar row. Texts stay out of the repr.
    """

    card_id: UUID
    card_type: str
    lemma: str = field(repr=False)
    cefr: str | None
    revision: int
    language: str
    state: str
    translations: dict[str, tuple[str, ...]] = field(repr=False)
    grammar: dict[str, GrammarForm | str] | None = field(repr=False)
    synonyms: SynonymList = field(repr=False)


@dataclass(frozen=True)
class OwnerSearchCard:
    """One card the owner's search found; ``preview`` is its first translation in the dictionary language.

    ``group_version`` is the version of the card's synonym group (None without one): a link
    button keeps it as ``expected_other_group_version`` (§4.4).
    """

    card_id: UUID
    lemma: str = field(repr=False)
    card_type: str
    cefr: str | None
    state: str
    preview: str | None = field(repr=False)
    group_version: int | None = None


@dataclass(frozen=True)
class OwnerSearch:
    cards: tuple[OwnerSearchCard, ...]
    more: bool


@dataclass(frozen=True)
class OwnerCommand:
    """The core's answer to an owner command: the fact's status and the card now.

    ``current`` is None on the repeat of a command whose actor is no longer the owner
    (project §4.4, ed. 3.3): the card is not shown to them.
    """

    status: str
    revision: int
    current: OwnerCard | None


def _text_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        raise CoreUnavailable()
    return tuple(value)


def _uuid(value: Any) -> UUID:
    if type(value) is not str:
        raise CoreUnavailable()
    try:
        return UUID(value)
    except ValueError:
        raise CoreUnavailable() from None


def _choice(value: Any, choices: tuple) -> Any:
    if value not in choices or (value is not None and type(value) is not str):
        raise CoreUnavailable()
    return value


def _grammar(value: Any, card_type: str) -> dict[str, GrammarForm | str] | None:
    """The card's grammar of the wire, or None; a column the type does not have is outside the contract."""
    if value is None:
        return None
    forms = _FORM_FIELDS.get(card_type)
    if forms is None:
        raise CoreUnavailable()
    status_only, plain = _STATUS_ONLY.get(card_type, ()), _PLAIN.get(card_type, ())
    keys = {*forms, *(f"{name}_status" for name in forms), *(f"{name}_status" for name in status_only), *plain}
    wire = _object(value, keys)
    grammar: dict[str, GrammarForm | str] = {}
    for name, closed in forms.items():
        form = _optional_text(wire[name])
        if form is not None and closed is not None and form not in closed:
            raise CoreUnavailable()
        grammar[name] = GrammarForm(_choice(wire[f"{name}_status"], FORM_STATUSES), form)
    for name in status_only:
        grammar[name] = GrammarForm(_choice(wire[f"{name}_status"], FORM_STATUSES))
    for name in plain:
        grammar[name] = _choice(wire[name], SEPARABILITY)
    return grammar


def _optional_version_of(value: Any) -> int | None:
    if value is None:
        return None
    if not is_version(value):
        raise CoreUnavailable()
    return int(value)


def _synonym_member(value: Any) -> SynonymMember:
    member = _object(value, {"card_id", "lemma", "type", "cefr", "state", "preview"})
    if type(member["lemma"]) is not str:
        raise CoreUnavailable()
    return SynonymMember(_uuid(member["card_id"]), member["lemma"], _choice(member["type"], CARD_TYPES),
                         _choice(member["cefr"], CEFR_LEVELS), _choice(member["state"], CARD_STATES),
                         _optional_text(member["preview"]))


def _synonym_list(value: Any, keys: set[str] = frozenset()) -> tuple[dict, SynonymList]:
    """A SynonymList (and the extra ``keys`` of a SynonymGroup); members only with a group, all distinct."""
    wire = _object(value, {"group_version", "members", *keys})
    if not isinstance(wire["members"], list):
        raise CoreUnavailable()
    version = _optional_version_of(wire["group_version"])
    members = tuple(_synonym_member(member) for member in wire["members"])
    if (version is None and members) or len({member.card_id for member in members}) != len(members):
        raise CoreUnavailable()
    return wire, SynonymList(version, members)


def _synonym_group(value: Any, card_id: UUID) -> SynonymGroup:
    wire, synonyms = _synonym_list(value, {"card_id", "card_type"})
    group = SynonymGroup(_uuid(wire["card_id"]), _choice(wire["card_type"], CARD_TYPES), synonyms)
    if group.card_id != card_id:
        raise CoreUnavailable()
    return group


def _owner_card(value: Any) -> OwnerCard:
    card = _object(value, {"card_id", "card_type", "lemma", "cefr", "card_revision", "language", "state",
                           "translations", "grammar", "synonyms"})
    translations = _object(card["translations"], set(OWNER_LANGUAGES))
    card_type = _choice(card["card_type"], CARD_TYPES)
    if type(card["lemma"]) is not str or not is_version(card["card_revision"]):
        raise CoreUnavailable()
    return OwnerCard(_uuid(card["card_id"]), card_type, card["lemma"], _choice(card["cefr"], CEFR_LEVELS),
                     int(card["card_revision"]), _choice(card["language"], OWNER_LANGUAGES),
                     _choice(card["state"], CARD_STATES),
                     {language: _text_list(translations[language]) for language in OWNER_LANGUAGES},
                     _grammar(card["grammar"], card_type), _synonym_list(card["synonyms"])[1])


def _search_card(value: Any) -> OwnerSearchCard:
    card = _object(value, {"card_id", "lemma", "type", "cefr", "state", "preview", "synonym_group_version"})
    if type(card["lemma"]) is not str:
        raise CoreUnavailable()
    return OwnerSearchCard(_uuid(card["card_id"]), card["lemma"], _choice(card["type"], CARD_TYPES),
                           _choice(card["cefr"], CEFR_LEVELS), _choice(card["state"], CARD_STATES),
                           _optional_text(card["preview"]), _optional_version_of(card["synonym_group_version"]))


def _fact(answer: dict, command_id: UUID) -> tuple[str, int]:
    """The status and object version of a command fact, checked against the command that was sent."""
    fact = _object(answer["command"], {"id", "status", "applied_at", "target_id", "object_version"})
    if (fact["id"] != str(command_id) or not is_version(fact["object_version"])
            or type(fact["applied_at"]) is not str or not is_optional_text(fact["target_id"])):
        raise CoreUnavailable()
    return _choice(fact["status"], COMMAND_STATUSES), int(fact["object_version"])


def _synonym_command(value: Any, command_id: UUID, card_id: UUID) -> SynonymCommand:
    answer = _object(value, {"command", "current"})
    status, version = _fact(answer, command_id)
    current = None if answer["current"] is None else _synonym_group(answer["current"], card_id)
    return SynonymCommand(status, version, current)


def _owner_search(value: Any, limit: int) -> OwnerSearch:
    answer = _object(value, {"cards", "more"})
    if not isinstance(answer["cards"], list) or type(answer["more"]) is not bool or len(answer["cards"]) > limit:
        raise CoreUnavailable()
    return OwnerSearch(tuple(_search_card(card) for card in answer["cards"]), answer["more"])


def _owner_command(value: Any, command_id: UUID, card_id: UUID) -> OwnerCommand:
    """The answer of one owner command, checked against the command that was sent."""
    answer = _object(value, {"command", "current"})
    status, version = _fact(answer, command_id)
    current = None if answer["current"] is None else _owner_card(answer["current"])
    if current is not None and current.card_id != card_id:
        # The answer is about another card than the command named.
        raise CoreUnavailable()
    return OwnerCommand(status, version, current)


def _card_reference(card_id: UUID) -> str:
    if not isinstance(card_id, UUID):
        raise TypeError("A card is named by its UUID")
    return str(card_id)


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


def _optional_version(value: int | None) -> str | None:
    return None if value is None else _version(value)


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

    async def owner_search(self, principal: TelegramPrincipal, *, query: str, limit: int = SEARCH_LIMIT_MAX,
                           card_type: str | None = None) -> OwnerSearch:
        """Published cards matching ``query`` (a word, a form or a card id), hidden ones included (Q07, Q08).

        A read: FORBIDDEN unless the actor is the active owner; nothing is written.
        """
        if type(query) is not str or not 1 <= len(query) <= QUERY_MAX_CHARACTERS:
            raise ValueError("A search query has 1 to 200 characters")
        if type(limit) is not int or not 1 <= limit <= SEARCH_LIMIT_MAX:
            raise ValueError("A search limit is 1 to 10")
        body = {**_actor(principal), "query": query, "limit": limit}
        if card_type is not None:
            body["type"] = card_type
        return _owner_search(await self.client.answer("/owner/cards/search", body), limit)

    async def owner_card(self, principal: TelegramPrincipal, *, card_id: UUID) -> OwnerCard:
        """One published card as the owner edits it; WRITE_UNAVAILABLE (409) for an absent or unpublished one."""
        body = {**_actor(principal), "card_id": _card_reference(card_id)}
        answer = _object(await self.client.answer("/owner/cards/read", body), {"current"})
        card = _owner_card(answer["current"])
        if card.card_id != card_id:
            raise CoreUnavailable()
        return card

    async def translation_op(self, principal: TelegramPrincipal, *, command_id: UUID, card_id: UUID,
                             language: str, op: str, index: int | None, text: str | None,
                             expected_card_revision: int) -> OwnerCommand:
        """One operation on the common translations of one language (R14-01, R12-06).

        ``replace``: index and text; ``append``: text; ``remove``, ``move_up``, ``move_down``:
        index, counted from 1 in the list at ``expected_card_revision``. The body names
        exactly what the caller passed: the same update gives the same body (§4.4).
        """
        if language not in OWNER_LANGUAGES or op not in TRANSLATION_OPERATIONS:
            raise ValueError("Unknown language or translation operation")
        if index is not None and (type(index) is not int or not 1 <= index <= INDEX_MAX):
            raise ValueError("A translation index is 1 to 99")
        if text is not None and type(text) is not str:
            raise TypeError("A translation is a text")
        body = {**_actor(principal), "command_id": str(UUID(str(command_id))), "card_id": _card_reference(card_id),
                "language": language, "op": op, "expected_card_revision": _version(expected_card_revision)}
        if index is not None:
            body["index"] = index
        if text is not None:
            body["text"] = text
        return _owner_command(await self.client.answer("/owner/cards/translation", body), command_id, card_id)

    async def grammar_save(self, principal: TelegramPrincipal, *, command_id: UUID, card_id: UUID,
                           field_name: str, value: dict | str, expected_card_revision: int) -> OwnerCommand:
        """One grammar field (R12-05): ``{"status", "value"}``, or the plain value of separability."""
        if isinstance(value, dict):
            if set(value) != {"status", "value"}:
                raise ValueError("A grammar form is {status, value}")
            value = {"status": value["status"], "value": value["value"]}
        elif type(value) is not str:
            raise TypeError("A grammar value is a form or a plain value")
        body = {**_actor(principal), "command_id": str(UUID(str(command_id))), "card_id": _card_reference(card_id),
                "field_name": field_name, "value": value,
                "expected_card_revision": _version(expected_card_revision)}
        return _owner_command(await self.client.answer("/owner/cards/grammar", body), command_id, card_id)

    # -- synonyms (stage B1b; core C4 /owner/synonyms/*) --------------------------------------

    async def synonym_candidates(self, principal: TelegramPrincipal, *, card_id: UUID) -> OwnerSearch:
        """A read: published cards of the type sharing a translation in the dictionary language (§3.5)."""
        body = {**_actor(principal), "card_id": _card_reference(card_id)}
        return _owner_search(await self.client.answer("/owner/synonyms/candidates", body), CANDIDATES_MAX)

    async def _synonym(self, path: str, principal: TelegramPrincipal, command_id: UUID, card_id: UUID,
                       fields: dict) -> SynonymCommand:
        body = {**_actor(principal), "command_id": str(UUID(str(command_id))), "card_id": _card_reference(card_id),
                **fields}
        return _synonym_command(await self.client.answer(path, body), command_id, card_id)

    async def synonym_link(self, principal: TelegramPrincipal, *, command_id: UUID, card_id: UUID,
                           other_card_id: UUID, expected_group_version: int | None,
                           expected_other_group_version: int | None) -> SynonymCommand:
        """Join ``other_card_id`` (and its group) to the group of ``card_id``; both versions as the button saw them."""
        return await self._synonym("/owner/synonyms/link", principal, command_id, card_id, {
            "other_card_id": _card_reference(other_card_id),
            "expected_group_version": _optional_version(expected_group_version),
            "expected_other_group_version": _optional_version(expected_other_group_version)})

    async def synonym_link_many(self, principal: TelegramPrincipal, *, command_id: UUID, card_id: UUID,
                                other_card_ids: tuple[UUID, ...], expected_group_version: int | None
                                ) -> SynonymCommand:
        """Join the confirmed candidates of "pick by translation"; only the group of ``card_id`` is compared."""
        if not 1 <= len(other_card_ids) <= LINK_MANY_MAX:
            raise ValueError("link_many takes 1 to 20 cards")
        return await self._synonym("/owner/synonyms/link_many", principal, command_id, card_id, {
            "other_card_ids": [_card_reference(other) for other in other_card_ids],
            "expected_group_version": _optional_version(expected_group_version)})

    async def synonym_unlink(self, principal: TelegramPrincipal, *, command_id: UUID, card_id: UUID, index: int,
                             expected_group_version: int) -> SynonymCommand:
        """Remove the ``index``-th member (from 1) of the group of ``card_id`` as the button listed it."""
        if type(index) is not int or not 1 <= index <= INDEX_MAX:
            raise ValueError("A member index is 1 to 99")
        return await self._synonym("/owner/synonyms/unlink", principal, command_id, card_id, {
            "index": index, "expected_group_version": _version(expected_group_version)})

    async def synonym_clear(self, principal: TelegramPrincipal, *, command_id: UUID, card_id: UUID,
                            expected_group_version: int) -> SynonymCommand:
        """Dissolve the whole group of ``card_id``."""
        return await self._synonym("/owner/synonyms/clear", principal, command_id, card_id, {
            "expected_group_version": _version(expected_group_version)})

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
