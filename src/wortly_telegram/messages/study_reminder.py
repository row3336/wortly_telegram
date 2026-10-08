"""Template of the study_reminder job: the learner's reminder of ready study items (T-D §4.3; TC §10, §13.2).

The core's builder (wortly.jobs.builders.study_reminder) sends the local date, the active
pair and the items ready at the preflight, each with the numbers of the Today screen
(Т36). This release has one item type, "learning_ready" (П12–П14). A type without a line
here fails rendering before any network call, so the contract test of the registry
catches a new core type that has no text.

The text follows answer 2.7 (keep the old bot's texts) where that is possible: the old
bot's Learning reminder (OLD bot/i18n.py "reminder_learning_ready", R-TG 2.01) without
its "/ need N" part, because the share of the group it named is gone (П14), and without
its last line, which pointed to the old bot's chat menu (К00-018). Like the old reminder
it has no button: whether reminders get the Mini App button is still open (Q20).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Language, OutgoingMessage, RenderError, Template


KIND = "study_reminder"

_FIELDS = frozenset({"local_date", "pair", "items"})
LEARNING_READY = "learning_ready"

# WORKING TEXT, wording pending the owner's approval (answer 2.7 keeps the old texts, but the
# old one had to be shortened, see above; T-TEXTS-PAGE): the old bot's heading and its
# "ready now" line.
HEADINGS: dict[Language, str] = {
    "ru": "Напоминание об изучении:",
    "uk": "Нагадування про вивчення:",
    "en": "Learning reminder:",
}
LEARNING_READY_LINES: dict[Language, str] = {
    "ru": "готово сейчас = {ready}",
    "uk": "готово зараз = {ready}",
    "en": "due now = {ready}",
}


def _learning_ready(item: Mapping[str, Any], language: Language) -> str:
    ready = item.get("ready")
    if set(item) != {"type", "ready"} or type(ready) is not int or ready < 1:
        raise RenderError("A learning_ready item names a positive count only")
    return LEARNING_READY_LINES[language].format(ready=ready)


_LINES = {LEARNING_READY: _learning_ready}


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    if set(body) != _FIELDS:
        raise RenderError("The study_reminder body has unexpected facts")
    items = body["items"]
    if type(body["local_date"]) is not str or type(body["pair"]) is not str:
        raise RenderError("A reminder names its local date and pair")
    if not isinstance(items, list) or not items:
        raise RenderError("A reminder names at least one ready item")
    lines = [HEADINGS[language]]
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") not in _LINES:
            raise RenderError("A reminder item has an unknown type")
        lines.append(_LINES[item["type"]](item, language))
    return OutgoingMessage("\n".join(lines))


TEMPLATE = Template(KIND, render, examples=(
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": LEARNING_READY, "ready": 3}]},
))
