"""The core's delivery loop with the Telegram channel on PostgreSQL wortly_test (design T-D §9.3).

Needs psycopg in the telegram environment, which the owner installs (§9.5 б); without
it the whole module is skipped. Each test owns a disposable schema of wortly_test on the
core's password-protected stand (pg_stand), as the core's own tests do. The real
access_approved builder prepares the facts, the real TelegramChannel sends them through
PTB over a fake transport.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import threading
import time
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

import httpx  # noqa: E402
from psycopg import sql  # noqa: E402

from conftest import TELEGRAM_SETTINGS  # noqa: E402
import pg_stand  # noqa: E402
from fakes.telegram_api import WEB_APP_URL  # noqa: E402
from wortly.jobs.kinds import discover as discover_kinds  # noqa: E402
from wortly.jobs.loop import DeliveryLoop, DeliveryLoopConfig, UnservedReport  # noqa: E402
from wortly.jobs.repository import JobRepository, lift_block  # noqa: E402
from wortly.jobs.retry_policy import RetryPolicyConfig  # noqa: E402
from wortly.storage import apply_migrations  # noqa: E402
from wortly_telegram.app import make_bot  # noqa: E402
from wortly_telegram.delivery.capacity import TelegramCapacity  # noqa: E402
from wortly_telegram.delivery.channel import TelegramChannel  # noqa: E402
from wortly_telegram.delivery.main import (  # noqa: E402
    DeliveryGate, loop_runner, reported_unserved,
)
from wortly_telegram.messages.registry import discover as discover_templates  # noqa: E402


NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
# Synthetic test values, not the deployment policy (G08/Q3).
D = timedelta(minutes=10)


@pytest.fixture
def database():
    parameters = pg_stand.connect_params()
    schema = f"wortly_telegram_test_{uuid4().hex}"
    with psycopg.connect(**parameters, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            @contextmanager
            def connections():
                with psycopg.connect(**parameters, autocommit=True) as connection:
                    connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    yield connection
            with connections() as connection:
                apply_migrations(connection)
            yield connections
        finally:
            assert schema.startswith("wortly_telegram_test_")
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def approved_recipient(connections, *, telegram_id: str = "4242", language: str = "uk"):
    profile, request = uuid4(), uuid4()
    with connections() as db, db.transaction():
        db.execute("INSERT INTO profiles(id,status,role) VALUES (%s,'active','user')", (profile,))
        db.execute("INSERT INTO user_coordination(profile_id) VALUES (%s)", (profile,))
        db.execute("INSERT INTO profile_settings(profile_id,ui_language,dictionary_language,time_zone,strict_case) "
                   "VALUES (%s,%s,'ru','UTC',TRUE)", (profile, language))
        db.execute("INSERT INTO identity_links(id,profile_id,provider,external_id) VALUES (%s,%s,'telegram',%s)",
                   (uuid4(), profile, telegram_id))
        db.execute("INSERT INTO access_requests(id,profile_id,status,decided_at) VALUES (%s,%s,'approved',%s)",
                   (request, profile, NOW))
    return profile, request


def enqueue(connections, repository, profile, request) -> object:
    with connections() as db, db.transaction():
        return repository.enqueue(db, logical_key=f"access_approved:{request}", profile_id=profile,
                                  kind="access_approved", available_at=NOW,
                                  payload={"request_id": str(request), "local_date": date(2026, 9, 29).isoformat()}
                                  ).job_id


def state(connections, job):
    with connections() as db:
        job_row = db.execute("SELECT status, retry_not_before FROM jobs WHERE id=%s", (job,)).fetchone()
        attempts = db.execute("SELECT status, failure_reason FROM delivery_attempts WHERE job_id=%s ORDER BY attempt",
                              (job,)).fetchall()
    return job_row, attempts


@pytest.fixture
def delivery(database, telegram, bot_loop):
    """Build delivery loops over the database; each has its own channel and gate, one Bot loop."""
    run = loop_runner(bot_loop)
    bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
    run(bot.initialize())
    stop = threading.Event()
    clock = [NOW]

    def make(owner: str = "delivery-a", *, bridge=None, report=None, monotonic=time.monotonic):
        capacity = TelegramCapacity(1000, wait=stop.wait)
        channel = TelegramChannel(bot, templates=discover_templates(), web_app_url=WEB_APP_URL,
                                  capacity=capacity, run=bridge or run)
        repository = JobRepository(database, clock=lambda: clock[0])
        config = DeliveryLoopConfig(owner=owner, lease_duration=timedelta(seconds=30),
                                    recovery_period=timedelta(minutes=1), poll_interval=timedelta(milliseconds=5),
                                    max_failed_preflights=3, retry=RetryPolicyConfig(nondelivery_delay=D))
        loop = DeliveryLoop(repository, config=config, kinds=discover_kinds(),
                            capacity=DeliveryGate(capacity, channel, stop), channel=channel,
                            monotonic=monotonic, unserved_report=report)
        return loop, repository, capacity

    yield make, stop
    run(bot.shutdown())


def test_sent(database, telegram, delivery):
    make, stop = delivery
    loop, repository, _ = make()
    profile, request = approved_recipient(database)
    job = enqueue(database, repository, profile, request)
    seen = []
    telegram.on_request = lambda call: seen.append(state(database, job)[1])
    result = loop.run_once(stop)
    assert result.status == "sent"
    [call] = telegram.calls_to("sendMessage")
    assert call.params["chat_id"] == "4242"
    assert call.params["text"].startswith("✅ Доступ підтверджено")
    # The fake, at the moment of the request, sees the committed sending attempt.
    assert seen == [[("sending", None)]]
    assert state(database, job) == (("completed", None), [("sent", None)])


def test_rate_limited_is_known_failed_with_retry_not_before(database, telegram, delivery):
    make, stop = delivery
    loop, repository, capacity = make()
    profile, request = approved_recipient(database)
    job = enqueue(database, repository, profile, request)
    telegram.fail("sendMessage", 429, "Too Many Requests", retry_after=30)
    assert loop.run_once(stop).status == "known_failed"
    assert state(database, job) == (("ready", NOW + timedelta(seconds=30)), [("known_failed", "rate_limited")])
    assert capacity.paused_for() > 25
    assert len(telegram.calls_to("sendMessage")) == 1


def test_a_confirmed_block_keeps_the_button_back_until_the_mark_is_lifted(database, telegram, delivery):
    # Before answer 2.5 any 403 was "blocked" and repeated after the delay; now the
    # confirmed block is not repeated and leaves the mark that a later contact lifts.
    # The login button is kept back meanwhile and delivered after the contact (2.28а).
    make, stop = delivery
    loop, repository, _ = make()
    profile, request = approved_recipient(database)
    job = enqueue(database, repository, profile, request)
    telegram.fail("sendMessage", 403, "Forbidden: bot was blocked by the user")
    loop.run_once(stop)
    assert state(database, job) == (("ready", NOW + timedelta(hours=24)), [("known_failed", "blocked")])
    with database() as db:
        assert db.execute("SELECT count(*) FROM telegram_blocks WHERE profile_id=%s",
                          (profile,)).fetchone()[0] == 1
    assert loop.run_once(stop).status == "idle"
    with database() as db, db.transaction(), db.cursor() as cursor:
        cursor.execute("SELECT 1 FROM user_coordination WHERE profile_id=%s FOR UPDATE", (profile,))
        assert lift_block(cursor, profile) == 1
    assert loop.run_once(stop).status == "sent"
    assert state(database, job) == (("completed", None), [("known_failed", "blocked"), ("sent", None)])
    assert len(telegram.calls_to("sendMessage")) == 2


def test_another_403_is_repeated_after_the_delay_and_leaves_no_mark(database, telegram, delivery):
    make, stop = delivery
    loop, repository, _ = make()
    profile, request = approved_recipient(database)
    job = enqueue(database, repository, profile, request)
    telegram.fail("sendMessage", 403, "Forbidden: bot can't initiate conversation with a user")
    loop.run_once(stop)
    assert state(database, job) == (("ready", NOW + D), [("known_failed", "forbidden")])
    with database() as db:
        assert db.execute("SELECT count(*) FROM telegram_blocks WHERE profile_id=%s",
                          (profile,)).fetchone()[0] == 0


def test_a_job_of_a_kind_without_a_builder_is_reported_once_and_does_not_stop_the_start(database, caplog):
    repository = JobRepository(database, clock=lambda: NOW)
    kinds = discover_kinds().names
    assert reported_unserved(kinds, repository, UnservedReport()) == frozenset()
    profile, _ = approved_recipient(database)
    with database() as db, db.transaction():
        repository.enqueue(db, logical_key="future_kind:1", profile_id=profile, kind="future_kind",
                           available_at=NOW, payload={})
    with caplog.at_level("ERROR", logger="wortly_telegram.delivery.main"):
        assert reported_unserved(kinds, repository, UnservedReport()) == frozenset({"future_kind"})
    lines = [record for record in caplog.records if "future_kind" in record.getMessage()]
    assert len(lines) == 1


def test_waiting_jobs_of_an_unknown_kind_stay_untouched_while_known_ones_are_delivered(database, telegram,
                                                                                        delivery, caplog):
    make, stop = delivery
    clock = [0.0]
    repository = JobRepository(database, clock=lambda: NOW)
    report = UnservedReport(monotonic=lambda: clock[0])
    loop, _, _ = make(report=report, monotonic=lambda: clock[0])
    profile, request = approved_recipient(database)
    with database() as db, db.transaction():
        waiting = repository.enqueue(db, logical_key="future_kind:1", profile_id=profile, kind="future_kind",
                                     available_at=NOW, payload={"secret": "body"}).job_id
    known = enqueue(database, repository, profile, request)
    before = state(database, waiting)
    with caplog.at_level("ERROR"):
        reported_unserved(discover_kinds().names, repository, report)
        for minutes in (0, 30, 61):
            clock[0] = minutes * 60.0
            loop.run_once(stop)
            loop.run_once(stop)
    assert state(database, known) == (("completed", None), [("sent", None)])
    assert state(database, waiting) == before == (("ready", None), [])
    messages = [record.getMessage() for record in caplog.records if record.levelname == "ERROR" and record.name != "wortly.trail"]
    # At start, then again only after more than an hour; the loop's polls share the start's report.
    assert messages == ["1 job kinds this release cannot deliver are waiting: future_kind"] * 2
    assert not any("body" in message or str(waiting) in message or str(profile) in message for message in messages)


def test_unknown_is_final_and_never_sent_again(database, telegram, delivery):
    make, stop = delivery
    loop, repository, _ = make()
    profile, request = approved_recipient(database)
    job = enqueue(database, repository, profile, request)
    telegram.raise_error("sendMessage", httpx.ReadTimeout("slow", request=None))
    assert loop.run_once(stop).status == "unknown"
    assert state(database, job) == (("failed", None), [("unknown", None)])
    assert loop.run_once(stop).status == "idle"
    assert len(telegram.calls_to("sendMessage")) == 1


def test_an_error_after_a_delivered_answer_is_unknown_with_one_request(database, telegram, delivery, bot_loop):
    make, stop = delivery
    run = loop_runner(bot_loop)

    def answered_then_failed(coroutine):
        run(coroutine)
        raise RuntimeError("lost before resolve")

    loop, repository, _ = make(bridge=answered_then_failed)
    profile, request = approved_recipient(database)
    job = enqueue(database, repository, profile, request)
    assert loop.run_once(stop).status == "unknown"
    assert state(database, job)[1] == [("unknown", None)]
    assert len(telegram.calls_to("sendMessage")) == 1


def test_two_delivery_processes_send_each_job_once(database, telegram, delivery):
    make, stop = delivery
    (first, repository, _), (second, _, _) = make("delivery-a"), make("delivery-b")
    jobs = []
    for number in range(3):
        profile, request = approved_recipient(database, telegram_id=str(5000 + number))
        jobs.append(enqueue(database, repository, profile, request))
    with ThreadPoolExecutor(2) as pool:
        for _ in range(3):
            list(pool.map(lambda loop: loop.run_once(stop), (first, second)))
    chats = sorted(call.params["chat_id"] for call in telegram.calls_to("sendMessage"))
    assert chats == ["5000", "5001", "5002"]
    assert all(state(database, job)[0][0] == "completed" for job in jobs)
