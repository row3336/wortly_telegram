"""The access_reminder template on the facts of the core's own builder (answer 2.6 а; C-ADAPTER-3).

The real builder (wortly.jobs.builders.access_reminder) runs over scripted rows instead
of a database. The reminder is the old bot's digest (R-TG 2.04): the count, one line per
pending request, the "show requests" button; without pending requests there is no
reminder at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from fakes.core_gateway import JOB_EVENT_QUERY, RECIPIENT_QUERY, ScriptedFacts, recipient_row
from wortly.jobs.builders import access_reminder as builder
from wortly.jobs.models import JobContext
from wortly_telegram.messages import LANGUAGES, RenderError
from wortly_telegram.messages.access_reminder import TEMPLATE
from wortly_telegram.messages.registry import discover


WINDOW_START = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
HEADS = {"ru": "⏰ {count} заявок на доступ ждут решения:",
         "uk": "⏰ {count} заявок на доступ очікують рішення:",
         "en": "⏰ {count} access request(s) pending decision:"}
BUTTONS = {"ru": "📋 показать заявки", "uk": "📋 показати заявки", "en": "📋 show requests"}


def row(external_id: str | None, first_name: str | None, username: str | None, minutes: int) -> dict:
    return {"id": uuid4(), "version": 1, "created_at": WINDOW_START - timedelta(minutes=minutes),
            "external_id": external_id, "first_name": first_name, "username": username}


ROWS = [row("5005", "Anna", "anna_k", 90), row("5006", "<b>Bob</b>", None, 30), row(None, None, None, 10)]


def built(language: str = "ru", *, rows=ROWS, checked_after=timedelta(minutes=1)):
    reader = ScriptedFacts(
        (JOB_EVENT_QUERY, [{"available_at": WINDOW_START}]),
        (RECIPIENT_QUERY, [recipient_row(language=language)]),
        ("FROM access_requests r", rows),
    )
    owner_id = uuid4()
    context = JobContext(uuid4(), owner_id, builder.job_key(owner_id, WINDOW_START), builder.KIND, 1,
                         WINDOW_START + checked_after)
    return builder.build(reader, context)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_reminder_counts_and_lists_the_requests_with_the_show_button(language):
    result = discover().prepare(builder.KIND, built(language).facts_json)
    assert result.chat_id == 4242
    assert result.message.text == "\n".join((
        HEADS[language].format(count=3), "• @anna_k | Anna | 5005", "• <b>Bob</b> | 5006", "• ?"))
    [[button]] = result.message.buttons
    assert (button.label, button.data) == (BUTTONS[language], "acc:l")
    assert result.message.app_button is False


def test_without_pending_requests_there_is_no_reminder():
    assert built(rows=[]) is None


def test_a_reminder_after_its_window_is_not_sent():
    # Root's decision А2 (C-ADAPTER-3): the next window's reminder replaces it.
    assert built(checked_after=timedelta(hours=2)) is None


@pytest.mark.parametrize("body", [
    {**TEMPLATE.examples[0], "count": 3},
    {**TEMPLATE.examples[0], "count": "2"},
    {**TEMPLATE.examples[0], "requests": []},
    {**TEMPLATE.examples[0], "requests": [{"request_id": "x"}, {}]},
    {"count": 2, "requests": TEMPLATE.examples[0]["requests"]},
])
def test_facts_outside_the_contract_fail_before_any_network_call(body):
    with pytest.raises(RenderError):
        TEMPLATE.render(body, "en")
