"""The owner's editor end to end: the bot -> the core's internal API -> PostgreSQL (project DEV-17 §7, B5).

The real Application, router, card editor, synonym editor and gateway of the bot; the Bot
API is FakeTelegram (no socket). The gateway's httpx transport calls the core's own
internal application (wortly.api.internal.create_internal_app) in process through
httpx.ASGITransport, on a disposable schema of wortly_test on the core's
password-protected stand (pg_stand). The Mini App's side is the core's public application
(wortly.api.app.create_app) in process as well, logged in as the core's tests do. Only
synthetic data; no port, no network (forbid_network).

Needs psycopg, fastapi and python-telegram-bot in one interpreter; without fastapi or
psycopg the whole module is skipped.

Scenarios (§7 B5, §3.1-§3.7, §4.4):
1. The owner finds a hidden card (no translation in the dictionary language) with /find
   and adds one: the card appears in the ordinary search of the Mini App.
2. An edit in the bot -> a Mini App write at the old revision gets 409 VERSION_CONFLICT
   and changes nothing; and the other way round, an old bot button after a Mini App edit
   gets the conflict notice with the card read afresh.
3. Synonyms through the bot: link (also a merge of two groups) and unlink keep the core's
   invariants (one group per card, one type, at least two members, card revisions
   untouched); the same press again is already_applied without "Saved".
4. A user who is not the owner gets the default answer and no owner route is called; an
   owner demoted between the bot's audience check and the write gets FORBIDDEN from the
   core and nothing changes; the repeat of an applied command after demotion shows nothing.
5. The core unavailable or answering outside the contract: CoreUnavailable, the bot says
   the change may have been saved, does not repeat the call, and nothing is written; and
   a write the core made whose answer was lost: the same answer, the write is there, and
   the same message again is already_applied.

No update may end in an exception the Application's error handler swallows (fixture
``failures``): otherwise "nothing is shown" would also pass for a failed handler.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

pytest.importorskip("fastapi")
psycopg = pytest.importorskip("psycopg")

import httpx  # noqa: E402
from psycopg import sql  # noqa: E402

from conftest import feed  # noqa: E402
import pg_stand  # noqa: E402
from fakes.core_gateway import core_settings, forbid_network  # noqa: E402
from fakes.telegram_api import WEB_APP_URL, FakeTelegram, callback_update, message_update, user  # noqa: E402
from wortly.api.app import CONTRACT_VERSION_HEADER, ApiConfiguration, create_app  # noqa: E402
from wortly.api.internal import InternalConfiguration, create_internal_app  # noqa: E402
from wortly.dictionary.catalog_writer import create_card  # noqa: E402
from wortly.dictionary.validation import Form  # noqa: E402
from wortly.identity.clients import ClientService  # noqa: E402
from wortly.identity.origin import CommandOrigin  # noqa: E402
from wortly.identity.profile_defaults import admit_profile_settings  # noqa: E402
from wortly.identity.service import IdentityService  # noqa: E402
from wortly.identity.telegram import TelegramIdentity  # noqa: E402
from wortly.storage import apply_migrations  # noqa: E402
from wortly_telegram.app import build_application  # noqa: E402
from wortly_telegram.card_view import ANSWERS, TYPE_LABELS, card_code  # noqa: E402
from wortly_telegram.gateway import build_gateway  # noqa: E402
from wortly_telegram.service import card_editor, synonyms  # noqa: E402
from wortly_telegram.start import TEXTS as START_TEXTS  # noqa: E402
from wortly_telegram.synonym_view import TYPE_CODES  # noqa: E402


SECRET = "synthetic-bot-b5-chain-secret"
OWNER_ID, USER_ID = 7501, 7502
OWNER, USER = user(OWNER_ID, language_code="ru"), user(USER_ID, language_code="ru")
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
# The answers of §3.7 as the project words them (working texts): a drift of the bot's texts shows here.
SAVED = "✅ Сохранено."
CONFLICT = "Карточку уже изменили (возможно, в Mini App). Показываю актуальные данные — повтори действие."
FORBIDDEN = "Это действие доступно только владельцу."
SILENT = "Ядро не ответило. Изменение могло сохраниться — нажми «Обновить», прежде чем повторять."
RU = ANSWERS["ru"]
OWNER_PATHS = ("/internal/v1/owner/cards/translation", "/internal/v1/owner/cards/grammar",
               "/internal/v1/owner/cards/search", "/internal/v1/owner/cards/read",
               "/internal/v1/owner/synonyms/link", "/internal/v1/owner/synonyms/link_many",
               "/internal/v1/owner/synonyms/unlink", "/internal/v1/owner/synonyms/clear",
               "/internal/v1/owner/synonyms/candidates")
TRANSLATION = "/internal/v1/owner/cards/translation"
CARD_TABLES = ("cards", "noun_grammar", "translations")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


class Failures(logging.Handler):
    """The records of the Application's error handler (app._log_failure): a handler that failed."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[str] = []

    def emit(self, record) -> None:
        self.records.append(record.getMessage())


@pytest.fixture(autouse=True)
def failures():
    """No update of any scenario may end in an exception PTB swallows: "nothing shown" must be silence."""
    handler = Failures()
    logger = logging.getLogger("wortly_telegram.app")
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
    assert handler.records == []


# -- the core on a disposable schema ----------------------------------------------------------

@pytest.fixture
def connections():
    parameters = pg_stand.connect_params()
    schema = f"wortly_telegram_test_{uuid4().hex}"
    with psycopg.connect(**parameters, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            @contextmanager
            def connect():
                with psycopg.connect(**parameters, autocommit=True) as connection:
                    connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    yield connection

            with connect() as connection:
                apply_migrations(connection)
                connection.execute("INSERT INTO access_policy(mode) VALUES ('manual') "
                                   "ON CONFLICT (singleton) DO UPDATE SET mode='manual'")
            yield connect
        finally:
            assert schema.startswith("wortly_telegram_test_")
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def identities(connections):
    # The 1-hour session only lets the test log the Mini App in; the bot never logs in.
    return IdentityService(connections, clock=lambda: NOW, session_lifetime=timedelta(hours=1))


class Tap(httpx.AsyncBaseTransport):
    """The bot's transport to the core's internal app; records paths, may act before one request."""

    def __init__(self, app) -> None:
        self.inner = httpx.ASGITransport(app=app)
        self.paths: list[str] = []
        self.before: dict[str, object] = {}
        # After the core answered: the answer may be lost on the way back (the write stays).
        self.after: dict[str, object] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        hook = self.before.pop(path, None)
        if hook is not None:
            answer = hook(request)
            if answer is not None:
                return answer
        response = await self.inner.handle_async_request(request)
        lost = self.after.pop(path, None)
        if lost is not None:
            await response.aread()
            return lost(response)
        return response

    def owner_calls(self) -> list[str]:
        return [path for path in self.paths if path in OWNER_PATHS]


@pytest.fixture
async def world(connections, identities):
    internal = create_internal_app(identity=identities, leases=None,
                                   config=InternalConfiguration(max_request_bytes=16384, service_secret=SECRET))
    tap = Tap(internal)
    gateway = build_gateway(core_settings(service_secret=SECRET), transport=tap)
    telegram = FakeTelegram()

    async def no_sleep(_seconds):
        return None
    functions = (*card_editor.service_functions(card_editor.EditorInputs()),
                 *synonyms.service_functions(synonyms.AddInputs(), synonyms.SynonymKeys()))
    application = build_application(telegram.bot(), gateway=gateway, web_app_url=WEB_APP_URL,
                                    functions=functions, sleep=no_sleep)
    await application.initialize()
    public = create_app(identity=identities, config=ApiConfiguration(max_request_bytes=16384, contract_version="1"),
                        clock=lambda: NOW)
    miniapp = httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://api.test",
                                headers={CONTRACT_VERSION_HEADER: public.state.contract_version})
    try:
        yield SimpleNamespace(db=connections, identities=identities, tap=tap, telegram=telegram,
                              app=application, miniapp=miniapp)
    finally:
        await miniapp.aclose()
        await application.shutdown()
        await gateway.aclose()


def person(w, external: int, *, role: str = "owner", dictionary: str = "ru"):
    """An admitted profile of this Telegram ID with its Mini App session and one live document."""
    login = w.identities.login(TelegramIdentity(str(external), "ru", NOW), browser_language="ru")
    profile, token = UUID(login["profile"]["profile_id"]), login["access_token"]
    with w.db() as db:
        db.execute("UPDATE profiles SET status='active', role=%s, current_access_request_id=NULL WHERE id=%s",
                   (role, profile))
        db.execute("DELETE FROM access_requests WHERE profile_id=%s", (profile,))
        with db.cursor() as cursor:
            admit_profile_settings(cursor, profile, "ru", dictionary)
        db.execute("UPDATE profile_settings SET ui_language='ru', dictionary_language=%s WHERE profile_id=%s",
                   (dictionary, profile))
    clients = ClientService(w.identities)
    installation, document = uuid4(), uuid4()
    proof = clients.register(token, installation, uuid4())["origin_proof"]
    assert clients.open(token, installation, proof, document, "UTC", "open")["status"] == "opened"
    return SimpleNamespace(id=profile, token=token, origin=CommandOrigin(installation, document, proof))


def noun(w, lemma: str, translations: dict | None = None) -> UUID:
    draft = {"type": "noun", "lemma": lemma, "translations": translations or {"ru": ["дом"]},
             "grammar": {"gender": Form("applicable", "neuter"), "singular": Form("applicable", lemma),
                         "plural": Form("applicable", lemma + "e"), "article": Form("applicable")}}
    with w.db() as db:
        topic = db.execute("SELECT id FROM topics WHERE number=1").fetchone()
        if topic is None:
            topic = (uuid4(),)
            db.execute("INSERT INTO topics(id,number,german_name,description) "
                       "VALUES (%s,1,'Synthetic','Synthetic')", topic)
        result = create_card(db, {**draft, "topic_ids": [topic[0]]}, now=NOW)
    assert result.available, result.issues
    return result.card_id


def card_rows(w, item: UUID) -> dict:
    with w.db() as db:
        rows = {table: sorted(map(str, db.execute(
            f"SELECT to_jsonb(x) FROM {table} x WHERE {'id' if table == 'cards' else 'card_id'}=%s",
            (item,)).fetchall())) for table in CARD_TABLES}
        rows["facts"] = db.execute("SELECT count(*) FROM command_facts").fetchone()[0]
        return rows


def groups(w) -> tuple[dict, dict]:
    with w.db() as db:
        members = {UUID(str(card)): UUID(str(group)) for card, group in
                   db.execute("SELECT card_id,group_id FROM card_synonyms").fetchall()}
        versions = {UUID(str(group)): version for group, version in
                    db.execute("SELECT id,version FROM synonym_groups").fetchall()}
    return members, versions


def revisions(w, *items: UUID) -> list[int]:
    with w.db() as db:
        return [db.execute("SELECT revision FROM cards WHERE id=%s", (item,)).fetchone()[0] for item in items]


def assert_group_invariants(w) -> None:
    """One group per card (the key), one type per group, at least two members, every group used."""
    members, versions = groups(w)
    by_group: dict[UUID, list[UUID]] = {}
    for card, group in members.items():
        by_group.setdefault(group, []).append(card)
    assert set(by_group) == set(versions)
    with w.db() as db:
        for cards in by_group.values():
            assert len(cards) >= 2
            types = {db.execute("SELECT type FROM cards WHERE id=%s", (card,)).fetchone()[0] for card in cards}
            assert len(types) == 1


# -- the bot's chat -------------------------------------------------------------------------

_presses = iter(range(1, 10**6))


def press(data: str, *, from_user: dict = OWNER, callback_id: str | None = None) -> dict:
    return callback_update(data, from_user=from_user, callback_id=callback_id or f"b5-{next(_presses)}")


_messages = iter(range(100, 10**6))


def say(text: str, *, from_user: dict = OWNER, message_id: int | None = None) -> dict:
    return message_update(text, from_user=from_user, message_id=message_id or next(_messages))


def answers(w) -> list:
    """Every sendMessage and editMessageText of the bot in order, as (text, [callback_data])."""
    shown = []
    for call in w.telegram.calls:
        if call.method in ("sendMessage", "editMessageText"):
            data = []
            if "reply_markup" in call.params:
                data = [button.get("callback_data") for row in call.json("reply_markup")["inline_keyboard"]
                        for button in row]
            shown.append((call.params.get("text"), data))
    return shown


def labels(w) -> list[str]:
    """The button labels of the bot's last message."""
    [call] = [call for call in w.telegram.calls if call.method in ("sendMessage", "editMessageText")][-1:]
    return [button["text"] for row in call.json("reply_markup")["inline_keyboard"] for button in row]


def without_saved(text: str) -> str:
    """The view of an applied answer: the same text an already_applied repeat shows (§3.7)."""
    assert text.startswith(f"{SAVED}\n\n"), text
    return text[len(SAVED) + 2:]


def last(w):
    return answers(w)[-1]


def button(w, prefix: str) -> str:
    """The callback_data of the last answer that starts with ``prefix`` (exactly one)."""
    [found] = [data for data in last(w)[1] if data and data.startswith(prefix)]
    return found


# -- the Mini App ----------------------------------------------------------------------------

def envelope(origin: CommandOrigin, kind: str, payload: dict) -> dict:
    return {"command_id": str(uuid4()), "kind": kind, "client_instance_id": str(origin.client_instance_id),
            "document_id": str(origin.document_id), "payload": payload}


async def command(w, someone, body: dict) -> httpx.Response:
    return await w.miniapp.post("/api/v1/commands", json=body, headers={
        "Authorization": f"Bearer {someone.token}", "X-Origin-Proof": someone.origin.proof})


async def opened(w, someone, item: UUID) -> UUID:
    reply = await command(w, someone, envelope(someone.origin, "context.open",
                                               {"card_id": str(item), "source": {"kind": "dictionary"}}))
    assert reply.status_code == 200, reply.text
    return UUID(reply.json()["command"]["context_id"])


async def common_save(w, someone, context: UUID, item: UUID, value: list[str], *, version: str,
                      language: str = "ru") -> httpx.Response:
    body = {**envelope(someone.origin, "field.save", {"card_id": str(item), "field_name": "translation",
                                                       "scope": "common", "language": language, "value": value}),
            "context_id": str(context), "expected_field_version": version}
    return await command(w, someone, body)


async def revision_seen(w, someone, item: UUID) -> str:
    reply = await w.miniapp.get(f"/api/v1/dictionary/cards/{item}/view",
                                headers={"Authorization": f"Bearer {someone.token}"})
    assert reply.status_code == 200, reply.text
    return reply.json()["current"]["content"]["card"]["revision"]


async def ordinary_search(w, someone, query: str) -> list[str]:
    reply = await w.miniapp.get("/api/v1/dictionary/search", params={"mode": "words", "query": query, "limit": 10},
                                headers={"Authorization": f"Bearer {someone.token}"})
    assert reply.status_code == 200, reply.text
    return [entry["card_id"] for entry in reply.json()["items"]]


# -- 1. the hidden card ----------------------------------------------------------------------

async def test_the_owner_finds_a_hidden_card_adds_a_translation_and_it_shows_in_the_ordinary_search(world):
    w = world
    # The owner's dictionary language is uk; the card has only ru: hidden (Q23, R03).
    owner = person(w, OWNER_ID, dictionary="uk")
    reader = person(w, USER_ID, role="user", dictionary="uk")
    item = noun(w, "Haus")
    assert await ordinary_search(w, owner, "Haus") == [] and await ordinary_search(w, reader, "Haus") == []

    await feed(w.app, say("/find Haus"))
    text, data = last(w)
    code = card_code(item)
    assert data == [f"ce:o:{code}"] and labels(w) == [f"Haus · {TYPE_LABELS['ru']['noun']} · скрыта"]
    await feed(w.app, press(f"ce:o:{code}"))
    assert "скрыта" in last(w)[0]
    await feed(w.app, press(f"ce:tl:uk:{code}"))
    add = button(w, "ce:ad:uk:")
    assert add == f"ce:ad:uk:{code}:1:0"
    await feed(w.app, press(add))
    assert last(w)[0] == RU["append_prompt"]
    message = next(_messages)
    await feed(w.app, say("будинок", message_id=message))
    text, data = last(w)
    assert text.startswith(SAVED) and "1. будинок" in text
    assert f"ce:ad:uk:{code}:2:1" in data
    applied = without_saved(text)

    assert await ordinary_search(w, owner, "Haus") == [str(item)]
    assert await ordinary_search(w, reader, "будинок") == [str(item)]
    with w.db() as db:
        assert db.execute("SELECT language,text FROM translations WHERE card_id=%s ORDER BY language",
                          (item,)).fetchall() == [("ru", "дом"), ("uk", "будинок")]
    # The same message delivered again: the same command, already_applied, no second translation.
    before = card_rows(w, item)
    await feed(w.app, say("будинок", message_id=message))
    assert last(w)[0] == applied
    assert card_rows(w, item) == before
    # The bot's search now marks it visible.
    await feed(w.app, say("/find Haus"))
    assert labels(w) == [f"Haus · {TYPE_LABELS['ru']['noun']}"]


# -- 2. the bot and the Mini App on one revision -------------------------------------------------

async def test_an_edit_in_the_bot_makes_a_mini_app_write_at_the_old_revision_a_conflict(world):
    w = world
    owner = person(w, OWNER_ID)
    item = noun(w, "Haus")
    code = card_code(item)
    context = await opened(w, owner, item)
    seen = await revision_seen(w, owner, item)
    assert seen == "1"

    await feed(w.app, press(f"ce:tl:ru:{code}"))
    await feed(w.app, press(button(w, "ce:ad:ru:")))
    await feed(w.app, say("жилище"))
    assert last(w)[0].startswith(SAVED)
    assert revisions(w, item) == [2]
    before = card_rows(w, item)

    stale = await common_save(w, owner, context, item, ["здание"], version=seen)
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["error"] == {"code": "VERSION_CONFLICT"} and body["command"]["status"] == "rejected"
    assert body["current"]["translations"] == ["дом", "жилище"] and body["current"]["card_revision"] == "2"
    assert card_rows(w, item)["translations"] == before["translations"]
    assert revisions(w, item) == [2]
    # At the revision it now knows the Mini App saves.
    fresh = await common_save(w, owner, context, item, ["дом", "жилище", "здание"], version="2")
    assert fresh.status_code == 200, fresh.text


async def test_an_old_bot_button_after_a_mini_app_edit_gets_the_conflict_and_the_card_afresh(world):
    w = world
    owner = person(w, OWNER_ID)
    item = noun(w, "Haus", {"ru": ["дом", "здание"]})
    code = card_code(item)
    await feed(w.app, press(f"ce:tl:ru:{code}"))
    old_down = button(w, f"ce:dn:ru:{code}:1:1:")
    context = await opened(w, owner, item)
    assert (await common_save(w, owner, context, item, ["жилище"], version="1")).status_code == 200
    before = card_rows(w, item)
    await feed(w.app, press(old_down))
    text, data = last(w)
    assert text.startswith(CONFLICT) and "1. жилище" in text and "здание" not in text
    assert f"ce:ad:ru:{code}:2:1" in data
    assert card_rows(w, item)["translations"] == before["translations"] and revisions(w, item) == [2]


# -- 3. synonyms ------------------------------------------------------------------------------

async def link_through_the_bot(w, card: UUID, query: str) -> str:
    """➕ on the card's group, the query, and the single [Link] button; returns its data."""
    code = card_code(card)
    await feed(w.app, press(f"sy:v:{code}"))
    await feed(w.app, press(button(w, "sy:ad:")))
    await feed(w.app, say(query))
    return button(w, "sy:pk:")


async def test_synonym_link_merge_and_unlink_through_the_bot_keep_the_core_invariants(world):
    w = world
    person(w, OWNER_ID)
    haus, heim, bau, hof = (noun(w, lemma) for lemma in ("Haus", "Heim", "Bau", "Hof"))
    others = noun(w, "Gehen")  # another lemma of the same type, never linked
    start = revisions(w, haus, heim, bau, hof)

    pick = await link_through_the_bot(w, haus, "Heim")
    callback = "b5-link-haus-heim"
    await feed(w.app, press(pick, callback_id=callback))
    text, _ = last(w)
    assert text.startswith(SAVED) and "Heim" in text and "Haus" in text
    applied = without_saved(text)
    members, versions = groups(w)
    assert members[haus] == members[heim] and len(versions) == 1
    snapshot = groups(w)
    # The same press delivered again: already_applied, no "Saved", nothing changes.
    await feed(w.app, press(pick, callback_id=callback))
    assert last(w)[0] == applied
    assert groups(w) == snapshot

    # Bau and Hof in another group, then Haus links Bau: the groups merge (no confirmation, §3.5).
    pick = await link_through_the_bot(w, bau, "Hof")
    await feed(w.app, press(pick))
    assert last(w)[0].startswith(SAVED)
    assert len(groups(w)[1]) == 2
    pick = await link_through_the_bot(w, haus, "Bau")
    merge = "b5-merge"
    await feed(w.app, press(pick, callback_id=merge))
    applied = without_saved(last(w)[0])
    members, versions = groups(w)
    assert len(versions) == 1 and {members[card] for card in (haus, heim, bau, hof)} == set(versions)
    assert others not in members
    assert_group_invariants(w)
    merged = groups(w)
    # The same merge press again, the groups changed meanwhile: the same body, already_applied (§4.4).
    await feed(w.app, press(pick, callback_id=merge))
    assert last(w)[0] == applied and groups(w) == merged

    # Unlink the member shown as number i: exactly that card leaves.
    await feed(w.app, press(f"sy:v:{card_code(haus)}"))
    removes = [data for data in last(w)[1] if data and data.startswith("sy:rm:")]
    shown = sorted((haus, heim, bau, hof))  # the core lists members by card id
    assert len(removes) == 4
    target = 2
    victim = shown[target - 1]
    unlink = "b5-unlink"
    [remove] = [data for data in removes if data.endswith(f":{target}")]
    await feed(w.app, press(remove, callback_id=unlink))
    applied = without_saved(last(w)[0])
    members, versions = groups(w)
    assert victim not in members and set(members) == set(shown) - {victim}
    after = groups(w)
    await feed(w.app, press(remove, callback_id=unlink))
    assert last(w)[0] == applied and groups(w) == after
    # A new press on the same (now stale) button: VERSION_CONFLICT, the group redrawn, nothing changes.
    await feed(w.app, press(remove))
    assert last(w)[0].startswith("Группу синонимов уже изменили") and groups(w) == after
    assert_group_invariants(w)

    # Unlink down to one: the group dissolves (invariant >= 2).
    for _ in range(2):
        still = sorted(groups(w)[0])[0]  # a card still in the group (haus itself may have left)
        await feed(w.app, press(f"sy:v:{card_code(still)}"))
        first = [data for data in last(w)[1] if data and data.startswith("sy:rm:")][0]
        await feed(w.app, press(first))
        assert last(w)[0].startswith(SAVED)
    assert groups(w) == ({}, {})
    # Synonyms never touch the cards' revisions.
    assert revisions(w, haus, heim, bau, hof) == start


# -- 4. not the owner -------------------------------------------------------------------------

async def test_a_user_who_is_not_the_owner_gets_the_default_answer_and_reaches_no_owner_route(world):
    w = world
    person(w, OWNER_ID)
    person(w, USER_ID, role="user")
    item = noun(w, "Haus")
    code = card_code(item)
    before = card_rows(w, item), groups(w)
    for update in (say("/find Haus", from_user=USER), press(f"ce:o:{code}", from_user=USER),
                   press(f"ce:ad:ru:{code}:1:1", from_user=USER), press(f"sy:v:{code}", from_user=USER),
                   press(f"sy:rm:{code}:1:1", from_user=USER), say("жилище", from_user=USER)):
        await feed(w.app, update)
        assert last(w)[0] == START_TEXTS["ru"]
    assert w.tap.owner_calls() == []
    assert (card_rows(w, item), groups(w)) == before


async def test_an_owner_demoted_before_the_write_is_forbidden_by_the_core_and_nothing_changes(world):
    w = world
    owner = person(w, OWNER_ID)
    item, other = noun(w, "Haus"), noun(w, "Heim")
    code = card_code(item)

    def demote(_request):
        with w.db() as db:
            db.execute("UPDATE profiles SET role='user' WHERE id=%s", (owner.id,))

    await feed(w.app, press(f"ce:tl:ru:{code}"))
    await feed(w.app, press(button(w, "ce:ad:ru:")))
    before = card_rows(w, item)
    # The router saw an owner; the core sees a user when the write arrives.
    w.tap.before[TRANSLATION] = demote
    await feed(w.app, say("жилище"))
    assert last(w)[0] == FORBIDDEN
    assert card_rows(w, item) == before

    with w.db() as db:
        db.execute("UPDATE profiles SET role='owner' WHERE id=%s", (owner.id,))
    pick = await link_through_the_bot(w, item, "Heim")
    w.tap.before["/internal/v1/owner/synonyms/link"] = demote
    await feed(w.app, press(pick))
    assert last(w)[0] == FORBIDDEN
    assert groups(w) == ({}, {}) and revisions(w, item, other) == [1, 1]


async def test_the_repeat_of_an_applied_command_after_demotion_shows_nothing(world, failures):
    w = world
    owner = person(w, OWNER_ID)
    item = noun(w, "Haus", {"ru": ["дом", "здание"]})
    code = card_code(item)
    await feed(w.app, press(f"ce:tl:ru:{code}"))
    down = button(w, f"ce:dn:ru:{code}:1:1:")
    await feed(w.app, press(down, callback_id="b5-down"))
    assert last(w)[0].startswith(SAVED)
    shown = len(answers(w))
    before = card_rows(w, item)

    def demote(_request):
        with w.db() as db:
            db.execute("UPDATE profiles SET role='user' WHERE id=%s", (owner.id,))
    w.tap.before[TRANSLATION] = demote
    await feed(w.app, press(down, callback_id="b5-down"))
    # already_applied with current=null (§4.4 ed. 3.3): no card for a former owner, no answer,
    # and handled, not failed: a failing handler shows nothing either.
    assert failures == []
    assert len(answers(w)) == shown
    assert card_rows(w, item) == before


async def test_the_repeat_of_a_synonym_command_after_demotion_shows_nothing(world, failures):
    w = world
    owner = person(w, OWNER_ID)
    haus, heim = noun(w, "Haus"), noun(w, "Heim")
    pick = await link_through_the_bot(w, haus, "Heim")
    await feed(w.app, press(pick, callback_id="b5-link-demoted"))
    assert last(w)[0].startswith(SAVED)
    shown = len(answers(w))
    linked = groups(w)

    def demote(_request):
        with w.db() as db:
            db.execute("UPDATE profiles SET role='user' WHERE id=%s", (owner.id,))
    w.tap.before["/internal/v1/owner/synonyms/link"] = demote
    await feed(w.app, press(pick, callback_id="b5-link-demoted"))
    # already_applied with current=null: the group is not shown to a former owner (synonyms.py).
    assert failures == []
    assert len(answers(w)) == shown
    assert groups(w) == linked


# -- 5. the core unavailable or outside the contract ---------------------------------------------

@pytest.mark.parametrize("answer", [
    pytest.param(lambda _request: (_ for _ in ()).throw(httpx.ConnectError("refused")), id="no-connection"),
    pytest.param(lambda _request: httpx.Response(200, json={"command": {"status": "applied"}}), id="outside-contract"),
    pytest.param(lambda _request: httpx.Response(502, text="<html>bad gateway</html>"), id="not-json"),
])
async def test_an_unavailable_core_or_an_answer_outside_the_contract_is_core_unavailable_without_a_write(
        world, answer):
    w = world
    person(w, OWNER_ID)
    item = noun(w, "Haus")
    code = card_code(item)
    await feed(w.app, press(f"ce:tl:ru:{code}"))
    await feed(w.app, press(button(w, "ce:ad:ru:")))
    before = card_rows(w, item)
    w.tap.paths.clear()
    w.tap.before[TRANSLATION] = answer
    await feed(w.app, say("жилище"))
    assert last(w)[0] == SILENT
    # One call, not repeated, and nothing reached the database.
    assert w.tap.owner_calls() == [TRANSLATION]
    assert card_rows(w, item) == before
    # «Обновить» shows the card as it is.
    await feed(w.app, press(f"ce:c:{code}"))
    assert "жилище" not in last(w)[0] and "дом" in last(w)[0]


async def test_a_write_the_core_made_whose_answer_was_lost_is_core_unavailable_and_its_repeat_already_applied(
        world):
    w = world
    person(w, OWNER_ID)
    item = noun(w, "Haus")
    code = card_code(item)
    await feed(w.app, press(f"ce:tl:ru:{code}"))
    await feed(w.app, press(button(w, "ce:ad:ru:")))
    w.tap.paths.clear()

    def lost(_response):
        raise httpx.ReadError("connection reset")
    # The real core applies the command; its answer never reaches the bot.
    w.tap.after[TRANSLATION] = lost
    message = next(_messages)
    await feed(w.app, say("жилище", message_id=message))
    assert last(w)[0] == SILENT
    assert w.tap.owner_calls() == [TRANSLATION]
    with w.db() as db:
        assert db.execute("SELECT text FROM translations WHERE card_id=%s AND language='ru' ORDER BY position",
                          (item,)).fetchall() == [("дом",), ("жилище",)]
    saved = card_rows(w, item)
    # The same message delivered again (a poller's retry): the same command, already_applied.
    await feed(w.app, say("жилище", message_id=message))
    text, _ = last(w)
    assert not text.startswith(SAVED) and not text.startswith(SILENT) and "2. жилище" in text
    assert card_rows(w, item) == saved and revisions(w, item) == [2]


async def test_a_search_answer_outside_the_contract_is_a_failed_read(world):
    w = world
    person(w, OWNER_ID)
    item = noun(w, "Haus")
    before = card_rows(w, item)
    w.tap.before["/internal/v1/owner/cards/search"] = lambda _request: httpx.Response(
        200, json={"cards": [{"card_id": "not-a-uuid"}], "more": False})
    await feed(w.app, say("/find Haus"))
    assert last(w)[0] == "Ядро не ответило. Попробуй ещё раз."
    assert card_rows(w, item) == before
