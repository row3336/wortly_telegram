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
from uuid import uuid4

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

    async def note_contact(self, principal: TelegramPrincipal, *, age_ms: int = 0) -> None:
        self._record("note_contact", principal, **({"age_ms": age_ms} if age_ms else {}))
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
        # The profile summary of each Telegram ID (ProfileSummary); absent: no profile.
        self.summaries: dict[str, dict] = {}
        # The owner editor's catalog (OwnerCard wire objects by card id) and its command facts.
        self.cards: dict[str, dict] = {}
        self.facts: dict[str, tuple[dict, dict]] = {}
        # Synonym groups (core C4, dictionary/synonyms.py): members and version of each group, the
        # group of each card, and one version sequence: a version never repeats.
        self.groups: dict[str, dict] = {}
        self.group_of: dict[str, str] = {}
        self.versions = 100

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
            return httpx.Response(200, json={"profile": self.summaries.get(body["actor_telegram_id"])})
        if path.startswith("/internal/v1/owner/cards/"):
            return self._owner_route(path.rsplit("/", 1)[1], body)
        if path.startswith("/internal/v1/owner/synonyms/"):
            return self._synonym_route(path.rsplit("/", 1)[1], body)
        if path == "/internal/v1/contact":
            return httpx.Response(204)
        if path == "/internal/v1/admission/pending_requests":
            return httpx.Response(200, json={"requests": list(self.pending)})
        if path in ("/internal/v1/admission/decide", "/internal/v1/admission/change_mode"):
            command = {"id": body["command_id"], "status": "applied", "applied_at": "2026-10-06T12:00:00Z",
                       "target_id": None, "object_version": "2"}
            return httpx.Response(200, json={"command": command, "current": {"mode": "manual", "version": "2"}})
        return envelope(404, "NOT_FOUND")

    # -- the owner's common editor, with the core's rules that the bot relies on (project DEV-17 §4) --

    def _owner_route(self, route: str, body: dict) -> httpx.Response:
        summary = self.summaries.get(body["actor_telegram_id"]) or {}
        if (summary.get("role"), summary.get("status")) != ("owner", "admitted"):
            return envelope(403, "FORBIDDEN")
        if route == "search":
            return self._search(body)
        card = self._card_out(self.cards.get(body["card_id"]))
        if route == "read":
            return httpx.Response(200, json={"current": card}) if card else envelope(409, "WRITE_UNAVAILABLE")
        command = body["command_id"]
        if command in self.facts:
            # The same command again (identity/service.py _prior): its fact, or the mismatch.
            prior, fact = self.facts[command]
            if prior != body:
                return envelope(409, "COMMAND_PAYLOAD_MISMATCH")
            return httpx.Response(200, json={"command": {**fact, "status": "already_applied"}, "current": card})
        if card is None:
            return envelope(409, "WRITE_UNAVAILABLE")
        if body["expected_card_revision"] != card["card_revision"]:
            return envelope(409, "VERSION_CONFLICT")
        try:
            changed = self._translation(card, body) if route == "translation" else self._grammar(card, body)
        except ValueError:
            return envelope(422, "VALIDATION_FAILED")
        changed["card_revision"] = str(int(card["card_revision"]) + 1)
        self.cards[body["card_id"]] = changed
        fact = {"id": command, "status": "applied", "applied_at": "2026-10-15T12:00:00Z",
                "target_id": body["card_id"], "object_version": changed["card_revision"]}
        self.facts[command] = (body, fact)
        return httpx.Response(200, json={"command": fact, "current": self._card_out(changed)})

    def _search(self, body: dict) -> httpx.Response:
        query = body["query"].strip().lower()
        found = [card for card in self.cards.values()
                 if body.get("type") in (None, card["card_type"])
                 and (query == card["card_id"] or query in card["lemma"].lower()
                      or any(query in text.lower() for texts in card["translations"].values() for text in texts))]
        found.sort(key=lambda card: card["card_id"])
        cards = [{"card_id": card["card_id"], "lemma": card["lemma"], "type": card["card_type"], "cefr": card["cefr"],
                  "state": card["state"], "preview": (card["translations"][card["language"]] or [None])[0],
                  "synonym_group_version": self._version_of(card["card_id"])}
                 for card in found[:body["limit"]]]
        return httpx.Response(200, json={"cards": cards, "more": len(found) > body["limit"]})

    # -- synonym groups (core C4) ---------------------------------------------------------------

    def _version_of(self, card_id: str) -> str | None:
        group = self.group_of.get(card_id)
        return None if group is None else str(self.groups[group]["version"])

    def _synonym_list(self, card_id: str) -> dict:
        group = self.group_of.get(card_id)
        if group is None:
            return {"group_version": None, "members": []}
        members = [self.cards[member] for member in sorted(self.groups[group]["members"])]
        return {"group_version": str(self.groups[group]["version"]),
                "members": [{"card_id": card["card_id"], "lemma": card["lemma"], "type": card["card_type"],
                             "cefr": card["cefr"], "state": card["state"],
                             "preview": (card["translations"][card["language"]] or [None])[0]} for card in members]}

    def _card_out(self, card: dict | None) -> dict | None:
        return None if card is None else {**card, "synonyms": self._synonym_list(card["card_id"])}

    def _next_version(self) -> int:
        self.versions += 1
        return self.versions

    def _group_out(self, card_id: str) -> dict:
        return {"card_id": card_id, "card_type": self.cards[card_id]["card_type"], **self._synonym_list(card_id)}

    def link_cards(self, *card_ids: str) -> int:
        """Put cards into one group, as the owner's earlier work (or another chat) did; returns its version."""
        group = self.group_of.get(card_ids[0]) or str(uuid4())
        self.groups.setdefault(group, {"members": set(), "version": 0})
        for card_id in card_ids:
            old = self.group_of.get(card_id)
            if old is not None and old != group:
                for member in self.groups.pop(old)["members"]:
                    self.group_of[member] = group
                    self.groups[group]["members"].add(member)
            self.group_of[card_id] = group
            self.groups[group]["members"].add(card_id)
        self.groups[group]["version"] = self._next_version()
        return self.groups[group]["version"]

    def _dissolve_small(self, group: str) -> None:
        if len(self.groups[group]["members"]) < 2:
            for member in self.groups.pop(group)["members"]:
                self.group_of.pop(member, None)

    def _synonym_route(self, route: str, body: dict) -> httpx.Response:
        summary = self.summaries.get(body["actor_telegram_id"]) or {}
        if (summary.get("role"), summary.get("status")) != ("owner", "admitted"):
            return envelope(403, "FORBIDDEN")
        card_id = body["card_id"]
        card = self.cards.get(card_id)
        if route == "candidates":
            if card is None:
                return envelope(409, "WRITE_UNAVAILABLE")
            own = {text.casefold() for text in card["translations"][card["language"]]}
            group = self.group_of.get(card_id)
            found = [other for other in sorted(self.cards.values(), key=lambda item: (item["lemma"], item["card_id"]))
                     if other["card_type"] == card["card_type"] and other["card_id"] != card_id
                     and (group is None or self.group_of.get(other["card_id"]) != group)
                     and own & {text.casefold() for text in other["translations"][other["language"]]}]
            cards = [{"card_id": other["card_id"], "lemma": other["lemma"], "type": other["card_type"],
                      "cefr": other["cefr"], "state": other["state"],
                      "preview": (other["translations"][other["language"]] or [None])[0],
                      "synonym_group_version": self._version_of(other["card_id"])} for other in found[:20]]
            return httpx.Response(200, json={"cards": cards, "more": len(found) > 20})
        command = body["command_id"]
        if command in self.facts:
            prior, fact = self.facts[command]
            if prior != body:
                return envelope(409, "COMMAND_PAYLOAD_MISMATCH")
            return httpx.Response(200, json={"command": {**fact, "status": "already_applied"},
                                             "current": self._group_out(card_id) if card else None})
        others = [body["other_card_id"]] if route == "link" else body.get("other_card_ids", [])
        if card is None or any(other not in self.cards for other in others):
            return envelope(409, "WRITE_UNAVAILABLE")
        if card_id in others or any(self.cards[other]["card_type"] != card["card_type"] for other in others):
            return envelope(422, "VALIDATION_FAILED")
        if self._version_of(card_id) != body["expected_group_version"]:
            return envelope(409, "VERSION_CONFLICT")
        if route == "link" and self._version_of(others[0]) != body["expected_other_group_version"]:
            return envelope(409, "VERSION_CONFLICT")
        group = self.group_of.get(card_id)
        if route in ("link", "link_many"):
            if group is not None and all(self.group_of.get(other) == group for other in others):
                version = self.groups[group]["version"]
            else:
                version = self.link_cards(card_id, *others)
        else:
            if route == "unlink":
                members = sorted(self.groups[group]["members"])
                if not 1 <= body["index"] <= len(members):
                    return envelope(422, "VALIDATION_FAILED")
                gone = members[body["index"] - 1]
                self.groups[group]["members"].discard(gone)
                self.group_of.pop(gone)
            else:
                for member in self.groups[group]["members"]:
                    self.group_of.pop(member)
                self.groups[group]["members"] = set()
            version = self._next_version()
            self.groups[group]["version"] = version
            self._dissolve_small(group)
        fact = {"id": command, "status": "applied", "applied_at": "2026-10-15T12:00:00Z", "target_id": card_id,
                "object_version": str(version)}
        self.facts[command] = (body, fact)
        return httpx.Response(200, json={"command": fact, "current": self._group_out(card_id)})

    @staticmethod
    def _translation(card: dict, body: dict) -> dict:
        texts = list(card["translations"][body["language"]])
        op, index, text = body["op"], body.get("index"), body.get("text")
        if op == "append":
            texts.append(text.strip())
        else:
            if not 1 <= index <= len(texts):
                raise ValueError
            at = index - 1
            if op == "replace":
                texts[at] = text.strip()
            elif op == "remove":
                if len(texts) == 1:
                    raise ValueError  # R12-06
                del texts[at]
            else:
                other = at - 1 if op == "move_up" else at + 1
                if not 0 <= other < len(texts):
                    raise ValueError
                texts[at], texts[other] = texts[other], texts[at]
        translations = {**card["translations"], body["language"]: texts}
        state = "visible" if translations[card["language"]] else "no_translation"
        return {**card, "translations": translations, "state": state}

    @staticmethod
    def _grammar(card: dict, body: dict) -> dict:
        name, value = body["field_name"], body["value"]
        grammar = dict(card["grammar"])
        if name == "separability":
            grammar[name] = value
        elif name == "article":
            grammar["article_status"] = value["status"]
        else:
            if value["status"] == "unknown" and name in ("preterite", "comparative", "superlative", "participle_ii"):
                raise ValueError  # incomplete on a published card (validation.py)
            grammar[name], grammar[f"{name}_status"] = value["value"], value["status"]
        return {**card, "grammar": grammar}

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


# -- the owner editor's cards in the core's wire shape (internal_models.OwnerCard) ----------

GRAMMAR_ROWS = {
    "noun": {"gender": "neuter", "gender_status": "applicable", "singular": "Haus", "singular_status": "applicable",
             "plural": "Häuser", "plural_status": "applicable", "article_status": "applicable"},
    "verb": {"infinitive": "gehen", "infinitive_status": "applicable", "preterite": "ging",
             "preterite_status": "applicable", "participle_ii": "gegangen", "participle_ii_status": "applicable",
             "auxiliary": "sein", "auxiliary_status": "applicable", "separability": "inseparable"},
    "adjective": {"positive": "gut", "positive_status": "applicable", "comparative": "besser",
                  "comparative_status": "applicable", "superlative": "am besten",
                  "superlative_status": "applicable"},
}


def owner_card_wire(card_id: str, *, card_type: str = "noun", lemma: str = "das Haus", revision: int = 7,
                    language: str = "ru", translations: dict | None = None, grammar: dict | None = None,
                    cefr: str | None = "A1") -> dict:
    """A published card as the core's /owner/cards/* routes answer it (synthetic values)."""
    texts = {"ru": ["дом", "здание"], "uk": ["будинок"], "en": []} if translations is None else translations
    texts = {language_: list(texts.get(language_, [])) for language_ in ("ru", "uk", "en")}
    row = dict(GRAMMAR_ROWS.get(card_type, {})) if grammar is None else grammar
    return {"card_id": card_id, "card_type": card_type, "lemma": lemma, "cefr": cefr,
            "card_revision": str(revision), "language": language,
            "state": "visible" if texts[language] else "no_translation", "translations": texts,
            "grammar": row or None, "synonyms": {"group_version": None, "members": []}}
