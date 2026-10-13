"""The delivery's side of the user-path trail (R3-03; events TZ §4.3, root decisions P3-4 and of 13.10).

No database, no socket: the real channel sends through PTB over the fake Bot API, as in
test_channel.py; a capture handler on the trail's own logger receives the lines. The
negative probes feed a snapshot full of markers (recipient, text, logical key, profile) and
look for them in every line.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from uuid import uuid4

import httpx
import pytest

from conftest import TELEGRAM_SETTINGS
from fakes.core_gateway import JOB_EVENT_QUERY, RECIPIENT_QUERY, ScriptedFacts, recipient_row
from fakes.jobs import envelope, snapshot
from fakes.telegram_api import WEB_APP_URL
from wortly.jobs.channel import FAILURE_REASONS, SENT, UNKNOWN, ChannelResult
from wortly.jobs.kinds import discover as discover_kinds
from wortly.jobs.loop import UnservedReport
from wortly.jobs.models import JobContext
from wortly.jobs.retry_policy import KIND_CLASSES, RetryPolicyConfig, next_attempt
from wortly.operations import trail as core_trail
from wortly_telegram import trail as bot_trail
from wortly_telegram.app import make_bot
from wortly_telegram.delivery.channel import TelegramChannel, attempt_outcome
from wortly_telegram.delivery.main import loop_runner, reported_unserved
from wortly_telegram.messages.registry import discover


class Capacity:
    def pause(self, duration: timedelta) -> None:
        pass


@pytest.fixture
def make_channel(telegram, bot_loop):
    run = loop_runner(bot_loop)
    bots = []

    def make(templates=None) -> TelegramChannel:
        bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
        run(bot.initialize())
        bots.append(bot)
        return TelegramChannel(bot, templates=templates or discover(), web_app_url=WEB_APP_URL,
                               capacity=Capacity(), run=run)

    yield make
    for bot in bots:
        run(bot.shutdown())


class OneText:
    """Renders any kind with the access_approved template: the trail test needs a sendable message, not the kind's text."""

    def __init__(self) -> None:
        self._real = discover()

    def prepare(self, kind, facts_json):
        return self._real.prepare("access_approved", facts_json)


def transport(kind):
    return lambda t: t.raise_error("sendMessage", kind("simulated", request=None))


# kind, script, outcome of the attempt (channel), its reason, the trail's outcome
ATTEMPTS = [
    ("access_approved", lambda t: None, "sent", None, "sent"),
    ("access_approved", lambda t: t.fail("sendMessage", 429, "Too Many Requests", retry_after=30),
     "known_failed", "rate_limited", "retry"),
    ("access_approved", transport(httpx.ConnectError), "known_failed", "not_sent", "retry"),
    ("access_approved", lambda t: t.fail("sendMessage", 403, "Forbidden: bot was blocked by the user"),
     "known_failed", "blocked", "retry"),  # a one-off job is kept back until the mark is lifted (2.28а)
    ("study_reminder", lambda t: t.fail("sendMessage", 403, "Forbidden: bot was blocked by the user"),
     "known_failed", "blocked", "failed"),  # a study job is closed by a confirmed block (answer 2.5)
    ("daily_report", lambda t: t.fail("sendMessage", 403, "Forbidden: bot was blocked by the user"),
     "known_failed", "blocked", "failed"),
    ("access_reminder", lambda t: t.fail("sendMessage", 403, "Forbidden: bot was blocked by the user"),
     "known_failed", "blocked", "failed"),
    ("access_approved", lambda t: t.fail("sendMessage", 403, "Forbidden"), "known_failed", "forbidden", "retry"),
    ("access_approved", lambda t: t.fail("sendMessage", 400, "Bad Request"), "known_failed", "rejected", "retry"),
    ("access_approved", lambda t: t.fail("sendMessage", 401, "Unauthorized"),
     "known_failed", "channel_config", "retry"),
    ("access_approved", lambda t: t.fail("sendMessage", 429, "Too Many Requests"), "unknown", None, "unknown"),
    ("access_approved", transport(httpx.ReadTimeout), "unknown", None, "unknown"),
    ("access_approved", lambda t: t.fail("sendMessage", 500, "Internal"), "unknown", None, "unknown"),
]


@pytest.mark.parametrize(("kind", "script", "outcome", "reason", "traced"), ATTEMPTS,
                         ids=[f"{row[0]}-{row[3] or row[2]}" for row in ATTEMPTS])
def test_each_attempt_writes_one_line_with_its_job_attempt_kind_outcome_and_reason(
        telegram, make_channel, trail_lines, kind, script, outcome, reason, traced):
    shot = snapshot(kind, envelope("access_approved"))
    script(telegram)
    result = make_channel(OneText()).deliver(shot)
    assert (result.outcome, result.reason) == (outcome, reason)
    [line] = trail_lines.parsed
    assert (line["ev"], line["comp"], line["lvl"]) == ("d.job.done", "delivery", "warning" if traced == "failed" else "info")
    assert (line["job"], line["att"], line["kind"], line["outcome"], line.get("reason"), line["attempt"]) == (
        str(shot.claim.job_id), str(shot.attempt_id), kind, traced, reason, 1)
    assert isinstance(line["ms"], int) and line["ms"] >= 0
    assert set(line) <= {"ts", "ev", "lvl", "ver", "comp", "job", "att", "kind", "outcome", "reason", "attempt", "ms"}
    assert "dropped" not in line


@pytest.mark.parametrize("facts", [envelope(language="de"), envelope(telegram_user_id="not-an-id"), {"schema": 1}],
                         ids=["unknown-language", "bad-recipient", "bare-envelope"])
def test_a_fault_before_the_network_is_a_line_with_the_class_and_no_request(telegram, make_channel, trail_lines, facts):
    result = make_channel().deliver(snapshot(facts=facts))
    assert result.reason == "pre_send_error" and telegram.calls_to("sendMessage") == []
    [line] = trail_lines.parsed
    assert (line["outcome"], line["reason"]) == ("retry", "pre_send_error")


def test_the_attempt_number_of_a_later_attempt_is_written(telegram, make_channel, trail_lines):
    shot = snapshot("access_approved")
    object.__setattr__(shot, "attempt", 3)
    make_channel().deliver(shot)
    [line] = trail_lines.parsed
    assert line["attempt"] == 3


# -- the outcome follows the core's own retry rule ----------------------------------------------

NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
POLICY = RetryPolicyConfig(nondelivery_delay=timedelta(minutes=10))


@pytest.mark.parametrize("kind", sorted(KIND_CLASSES))
@pytest.mark.parametrize("reason", FAILURE_REASONS)
def test_failed_is_exactly_the_proven_nondelivery_the_core_closes(kind, reason):
    result = ChannelResult("known_failed", reason, timedelta(seconds=5) if reason == "rate_limited" else None)
    # event_at = now: a kept-back one-off job has its whole 24 hours left, so it is retried.
    closed = next_attempt(kind, result, NOW, POLICY, event_at=NOW) is None
    assert attempt_outcome(kind, result) == ("failed" if closed else "retry")


def test_sent_and_unknown_never_become_retry_or_failed():
    for kind in KIND_CLASSES:
        assert attempt_outcome(kind, SENT) == "sent"
        assert attempt_outcome(kind, UNKNOWN) == "unknown"


def test_the_delivery_reasons_are_all_closed_values_of_the_writer():
    assert set(FAILURE_REASONS) <= core_trail.REASONS


def test_a_legacy_result_without_a_class_is_a_retry():
    assert attempt_outcome("access_approved", ChannelResult("known_failed")) == "retry"


def test_a_failure_of_the_trail_never_changes_the_result_of_the_attempt(telegram, make_channel, monkeypatch):
    from wortly_telegram.delivery import channel as channel_module

    def broken(*_args):
        raise RuntimeError("trail broke")

    monkeypatch.setattr(channel_module, "attempt_outcome", broken)
    telegram.fail("sendMessage", 400, "Bad Request")
    result = make_channel().deliver(snapshot("access_approved"))
    assert (result.outcome, result.reason) == ("known_failed", "rejected")


# -- P3-4: a job in the channel always belongs to an admitted profile --------------------------

@pytest.mark.parametrize("status", ["pending", "rejected", "deleted"])
@pytest.mark.parametrize("kind", sorted(discover_kinds().names))
def test_no_builder_makes_a_snapshot_for_a_profile_that_is_not_active(kind, status):
    """The channel never sees such a job, so the ``job`` of its line is never a not-admitted profile's."""
    payload = {"request_id": str(uuid4()), "subject_profile_id": str(uuid4()), "inbox_id": str(uuid4()),
               "local_date": "2026-10-13", "week_start": "2026-10-12", "utc_date": "2026-10-13",
               "window_start": "2026-10-13T08:00:00Z"}
    reader = ScriptedFacts(
        (JOB_EVENT_QUERY, [{"payload": payload, "available_at": NOW}]),
        (RECIPIENT_QUERY, [recipient_row(status=status, role="owner")]))
    context = JobContext(uuid4(), uuid4(), f"{kind}:probe", kind, 1, NOW + timedelta(minutes=1))
    # A query beyond the recipient's would raise in ScriptedFacts: the builder stops at the status.
    assert discover_kinds().build(reader, context) is None


def test_every_kind_the_core_can_enqueue_is_known_to_the_trail():
    assert discover_kinds().names <= core_trail.JOB_KINDS


# -- d.unserved --------------------------------------------------------------------------------

class Waiting:
    def __init__(self, kinds) -> None:
        self.waiting = frozenset(kinds)

    def unserved_kinds(self, kinds):
        return self.waiting - frozenset(kinds)


def test_unserved_kinds_are_one_line_per_kind_with_the_size_of_the_set(trail_lines):
    report = UnservedReport(monotonic=lambda: 0.0)
    reported = reported_unserved(frozenset({"access_approved"}), Waiting({"daily_report", "weekly_report"}), report)
    assert reported == frozenset({"daily_report", "weekly_report"})
    daily, weekly = trail_lines.events("d.unserved")
    assert (daily["kind"], weekly["kind"], daily["n"], weekly["n"]) == ("daily_report", "weekly_report", 2, 2)
    assert (daily["comp"], daily["lvl"]) == ("delivery", "error")
    assert set(daily) == {"ts", "ev", "lvl", "ver", "comp", "kind", "n"}


def test_nothing_is_written_when_every_kind_is_served(trail_lines):
    kinds = discover_kinds().names
    report = UnservedReport(monotonic=lambda: 0.0)
    assert reported_unserved(kinds, Waiting(kinds), report) == frozenset()
    report.note(frozenset())
    assert trail_lines.events("d.unserved") == [] and trail_lines.parsed == []


def test_a_kind_the_trail_does_not_know_is_counted_not_written(trail_lines):
    reported_unserved(frozenset(), Waiting({"Marker Kind Q"}), UnservedReport(monotonic=lambda: 0.0))
    [line] = trail_lines.events("d.unserved")
    assert "kind" not in line and line["n"] == 1 and line["dropped"] == 1
    assert "Marker" not in trail_lines.everything


def test_the_same_set_is_not_written_again_within_the_hour(trail_lines):
    now = [0.0]
    report = UnservedReport(monotonic=lambda: now[0])
    for now[0] in (0.0, 600.0, 3000.0):
        report.note(frozenset({"daily_report"}))
    assert len(trail_lines.events("d.unserved")) == 1
    now[0] = 3601.0
    report.note(frozenset({"daily_report"}))
    assert len(trail_lines.events("d.unserved")) == 2


# -- negative probes ------------------------------------------------------------------------------

CHAT, TEXT = "7700123456", "MarkerBodyQ Katze"


def test_no_recipient_text_key_profile_or_snapshot_is_written_for_any_attempt(telegram, make_channel, trail_lines):
    shots = []
    for index, (kind, script, *_rest) in enumerate(ATTEMPTS):
        shot = snapshot(kind, envelope("access_approved", telegram_user_id=CHAT))
        shots.append(shot)
        script(telegram)
        make_channel(OneText()).deliver(shot)
    assert len(trail_lines.parsed) == len(ATTEMPTS)
    text = trail_lines.everything
    for shot in shots:
        for secret in (str(shot.claim.profile_id), shot.context.logical_key, shot.facts.facts_json,
                       shot.facts.versions_json, str(shot.context.profile_id)):
            assert secret not in text, secret
    for marker in (CHAT, "chat_id", "logical_key", "snapshot", "facts", "telegram_user_id", "recipient", '"prof"',
                   '"cmd"', "Forbidden", "Too Many", "Bad Request", "access_approved:test"):
        assert marker not in text, marker
    for line in trail_lines.parsed:
        assert "dropped" not in line and "truncated" not in line


def test_the_delivery_writes_no_field_of_a_profile_even_when_the_adapter_is_asked_to(trail_lines):
    bot_trail.emit("d.job.done", component="delivery", job=uuid4(), att=uuid4(), kind="access_approved",
                   outcome="sent", attempt=1, ms=3, prof=uuid4(), chat_id=5005, logical_key="k", snapshot="s")
    [line] = trail_lines.parsed
    assert not ({"prof", "chat_id", "logical_key", "snapshot"} & set(line))
    assert re.fullmatch(r"[0-9a-f-]{36}", line["job"])
