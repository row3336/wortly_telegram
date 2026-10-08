"""The access_approved template on the facts of the core's own builder (К02-028; answer 2.5, C-JOBS-2).

The real builder (wortly.jobs.builders.access_approved) runs over scripted rows instead
of a database, so the test holds in both interpreters (design §9.5). What it writes is
what the template must render: an empty body, the recipient's language and Telegram ID.
The builder's closing rules (24 hours, not approved, blocked) are the core's tests; here
only the boundary the template depends on is checked: a closed job has no facts at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from fakes.core_gateway import JOB_EVENT_QUERY, RECIPIENT_QUERY, ScriptedFacts, recipient_row
from wortly.jobs.builders import access_approved as builder
from wortly.jobs.models import JobContext
from wortly_telegram.messages import LANGUAGES
from wortly_telegram.messages.access_approved import TEXTS
from wortly_telegram.messages.registry import discover


APPROVED_AT = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
REQUEST_ID = uuid4()


def facts(language: str, *, checked_after: timedelta = timedelta(minutes=5), status: str = "approved"):
    reader = ScriptedFacts(
        (JOB_EVENT_QUERY, [{"payload": {"request_id": str(REQUEST_ID)}, "available_at": APPROVED_AT}]),
        (RECIPIENT_QUERY, [recipient_row(language=language, role="user")]),
        ("FROM access_requests", [{"status": status, "version": 2}]),
    )
    context = JobContext(uuid4(), uuid4(), f"access-approved:{REQUEST_ID}", builder.KIND, 1,
                         APPROVED_AT + checked_after)
    return builder.build(reader, context)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_builder_facts_render_the_notice_and_the_app_button(language):
    prepared = discover().prepare(builder.KIND, facts(language).facts_json)
    assert prepared.chat_id == 4242
    assert prepared.language == language
    assert prepared.message.text == TEXTS[language]
    assert prepared.message.app_button is True


def test_the_working_texts_of_the_old_bot_are_kept():
    # Answer 2.7: the old bot's texts stay (OLD bot/i18n.py "access_approved_notice").
    assert TEXTS == {"ru": "✅ Доступ подтверждён! Нажмите «Старт».",
                     "uk": "✅ Доступ підтверджено! Натисніть «Старт».",
                     "en": "✅ Access granted! Press Start."}


@pytest.mark.parametrize(("checked_after", "status"), [
    (timedelta(hours=24, microseconds=1), "approved"),  # answer 2.5, part 2: past 24 hours the job is closed
    (timedelta(minutes=5), "rejected"),
])
def test_a_closed_job_leaves_nothing_to_render(checked_after, status):
    assert facts("ru", checked_after=checked_after, status=status) is None


def test_a_notice_checked_at_exactly_24_hours_is_still_rendered():
    # Owner decision 2.19и: the 24-hour boundary is inclusive; only a later check closes the job.
    assert facts("ru", checked_after=timedelta(hours=24)) is not None
