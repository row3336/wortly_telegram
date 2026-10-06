"""Template of the access_reminder job: the owner's two-hourly digest of pending requests (answer 2.6 а).

The core's builder (wortly.jobs.builders.access_reminder) sends window_start, count and
the pending requests, oldest first, each in the access_request facts' shape. As the old
bot's reminder (OLD bot/jobs/access_reminders.py:34–69, R-TG 2.04): a count line, one
line per request and the "show requests" button, which lists the requests with their
decision buttons (service/access.py). The digest itself has no decision buttons.

Names and usernames are placed as they are, in plain text (ВП-16; access_request).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import CallbackButton, Language, OutgoingMessage, RenderError, Template
from .access_request import CALLBACK_PREFIX, EXAMPLE, SHOW_REQUESTS, pending_request


KIND = "access_reminder"

_FIELDS = frozenset({"window_start", "count", "requests"})
SHOW_REQUESTS_DATA = f"{CALLBACK_PREFIX}:{SHOW_REQUESTS}"

# WORKING TEXT (not set by the specification): the old bot's count line (answer 2.7; OLD
# bot/i18n.py "access_reminder_owner"), kept without plural forms as it was; final wording
# waits for the owner (T-TEXTS-PAGE).
PENDING_DECISION: dict[Language, str] = {
    "ru": "заявок на доступ ждут решения",
    "uk": "заявок на доступ очікують рішення",
    "en": "access request(s) pending decision",
}
# WORKING TEXT (not set by the specification): the old bot's button (answer 2.7; OLD
# bot/i18n.py "access_show_requests"); final wording waits for the owner (T-TEXTS-PAGE).
SHOW_REQUESTS_LABELS: dict[Language, str] = {
    "ru": "📋 показать заявки",
    "uk": "📋 показати заявки",
    "en": "📋 show requests",
}


def count_line(count: int, language: Language) -> str:
    return f"⏰ {count} {PENDING_DECISION[language]}:"


def request_line(label: str) -> str:
    return f"• {label}"


def show_requests_button(language: Language) -> CallbackButton:
    return CallbackButton(SHOW_REQUESTS_LABELS[language], SHOW_REQUESTS_DATA)


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    if set(body) != _FIELDS:
        raise RenderError("The access_reminder body has unexpected facts")
    count, requests = body["count"], body["requests"]
    if type(body["window_start"]) is not str or not isinstance(requests, list) or not requests:
        raise RenderError("A reminder names its window and at least one request")
    if type(count) is not int or count != len(requests):
        raise RenderError("The reminder's count is not the number of its requests")
    lines = [count_line(count, language)]
    lines.extend(request_line(pending_request(item)[2]) for item in requests)
    return OutgoingMessage("\n".join(lines), buttons=((show_requests_button(language),),))


TEMPLATE = Template(KIND, render, examples=(
    {"window_start": "2026-10-06T14:00:00Z", "count": 2, "requests": [
        EXAMPLE,
        {**EXAMPLE, "request_id": "5d1a7c3e-0000-4000-8000-000000000012",
         "applicant": {"telegram_user_id": "4245", "first_name": "Bob", "username": None}},
    ]},
))
