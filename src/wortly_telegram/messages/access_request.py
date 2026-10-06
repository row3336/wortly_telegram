"""Template of the access_request job: a new access request for the owner (answer 2.6 а, б; T-D §5.2).

The core's builder (wortly.jobs.builders.access_request) sends one pending request:
request_id, request_version (the decision's expected_version), the applicant's
Telegram ID, first name and username (either may be null; the username has no "@"),
and requested_at. The message names the applicant as the old bot did (OLD
bot/handlers/access.py:64–90, R-TG 2.07) and carries the three decision buttons.

The applicant's name and username are untrusted: the channel sends plain text without
parse_mode (ВП-16), so they are placed as they are, nothing escapes them and nothing here
adds markup. callback_data holds object references only, the request and its version,
never the actor (§5.3 п.1, §7.1 п.3): the bot's access service (service/access.py) takes
the actor from the update.

The applicant line and the buttons are shared by the reminder (access_reminder) and the
owner's list of pending requests (service/access.py).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from ..gateway import is_optional_text, is_version
from . import CallbackButton, Language, OutgoingMessage, RenderError, Template


KIND = "access_request"

# callback_data "acc:<action>:<request_id>:<version>"; the longest is 62 bytes (≤ 64).
CALLBACK_PREFIX = "acc"
APPROVE = "a"
REJECT = "r"
REJECT_WITH_REASON = "w"
SHOW_REQUESTS = "l"

_FIELDS = frozenset({"request_id", "request_version", "applicant", "requested_at"})
_APPLICANT_FIELDS = frozenset({"telegram_user_id", "first_name", "username"})
_APPLICANT_ORDER = ("telegram_user_id", "first_name", "username")

# WORKING TEXT (not set by the specification): the old bot's request line (answer 2.7; OLD
# bot/i18n.py "access_request_owner"); final wording waits for the owner (T-TEXTS-PAGE).
REQUESTS_ACCESS: dict[Language, str] = {
    "ru": "запрашивает доступ на использование бота",
    "uk": "запитує доступ до використання бота",
    "en": "requests access to the bot",
}
# WORKING TEXT (not set by the specification): the old bot's button (answer 2.7; OLD
# bot/i18n.py "access_approve_btn"); final wording waits for the owner (T-TEXTS-PAGE).
APPROVE_LABELS: dict[Language, str] = {"ru": "✅ Разрешить", "uk": "✅ Дозволити", "en": "✅ Approve"}
# WORKING TEXT (not set by the specification): the old bot's button (answer 2.7; OLD
# bot/i18n.py "access_reject_btn"); final wording waits for the owner (T-TEXTS-PAGE).
REJECT_LABELS: dict[Language, str] = {"ru": "❌ Отклонить", "uk": "❌ Відхилити", "en": "❌ Reject"}
# WORKING TEXT (not set by the specification): the button of answer 2.6 б, new (the old bot
# had none); final wording waits for the owner (T-TEXTS-PAGE).
REJECT_WITH_REASON_LABELS: dict[Language, str] = {
    "ru": "✍️ Отклонить с причиной",
    "uk": "✍️ Відхилити з причиною",
    "en": "✍️ Reject with a reason",
}


def callback_data(action: str, request_id: UUID, version: int) -> str:
    return f"{CALLBACK_PREFIX}:{action}:{request_id}:{version}"


def applicant_label(telegram_user_id: str | None, first_name: str | None, username: str | None) -> str:
    """The applicant as "@username | name | id", from the parts the core has, as they are.

    Without any part the label is "?", as the old bot's (OLD bot/handlers/access.py _user_tag).
    """
    parts = [f"@{username}" if username else None, first_name or None, telegram_user_id or None]
    return " | ".join(part for part in parts if part) or "?"


def decision_buttons(request_id: UUID, version: int, language: Language) -> tuple[tuple[CallbackButton, ...], ...]:
    """The three decision buttons of one request: approve and reject, then reject with a reason."""
    return ((CallbackButton(APPROVE_LABELS[language], callback_data(APPROVE, request_id, version)),
             CallbackButton(REJECT_LABELS[language], callback_data(REJECT, request_id, version))),
            (CallbackButton(REJECT_WITH_REASON_LABELS[language],
                            callback_data(REJECT_WITH_REASON, request_id, version)),))


def pending_request(body: Mapping[str, Any]) -> tuple[UUID, int, str]:
    """Request ID, version and applicant label of one pending request in the facts' shape."""
    if not isinstance(body, Mapping) or set(body) != _FIELDS:
        raise RenderError("A pending request has unexpected facts")
    applicant = body["applicant"]
    if not isinstance(applicant, Mapping) or set(applicant) != _APPLICANT_FIELDS:
        raise RenderError("A pending request's applicant has unexpected facts")
    version = body["request_version"]
    if not is_version(version):
        raise RenderError("A pending request's version is not a positive integer")
    if type(body["request_id"]) is not str or type(body["requested_at"]) is not str:
        raise RenderError("A pending request needs its ID and time")
    try:
        request_id = UUID(body["request_id"])
    except ValueError:
        raise RenderError("A pending request's ID is not a UUID") from None
    fields = [applicant[name] for name in _APPLICANT_ORDER]
    if not all(is_optional_text(value) for value in fields):
        raise RenderError("An applicant's field must be a string or null")
    label = applicant_label(*fields)
    return request_id, int(version), label


def request_text(label: str, language: Language) -> str:
    return f"👤 ({label}) {REQUESTS_ACCESS[language]}"


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    request_id, version, label = pending_request(body)
    return OutgoingMessage(request_text(label, language), buttons=decision_buttons(request_id, version, language))


EXAMPLE = {"request_id": "5d1a7c3e-0000-4000-8000-000000000011", "request_version": "1",
           "applicant": {"telegram_user_id": "4243", "first_name": "Anna", "username": "anna_k"},
           "requested_at": "2026-10-06T12:00:00Z"}

TEMPLATE = Template(KIND, render, examples=(
    EXAMPLE,
    {**EXAMPLE, "applicant": {"telegram_user_id": "4244", "first_name": None, "username": None}},
))
