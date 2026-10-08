"""Template of the study_reminder job: the learner's reminder of ready study items (T-D §4.3; TC §10, §13.2).

The core's builder (wortly.jobs.builders.study_reminder) sends the local date, the active
pair and the items ready at the preflight, each with the numbers of the Today screen
(Т36). This release has one item type, "learning_ready" (П12–П14). A type without a line
here fails rendering before any network call, so the contract test of the registry
catches a new core type that has no text.

The text is the owner's (S06): a heading line and a line with the number of ready cards,
the count the core sends (the Today screen's, by the accepted Learning readiness rules
and the chosen material). The old bot's share of the group ("need N", gone with П14) and
its line to the old chat menu (К00-018) do not come back.

The message carries the plain Mini App button with its own label (S04). It opens the
configured address as the start button does, with nothing in the query, so the Mini App
opens at its usual entry: pressing it neither starts a session nor ends the current one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Language, OutgoingMessage, RenderError, Template


KIND = "study_reminder"

_FIELDS = frozenset({"local_date", "pair", "items"})
LEARNING_READY = "learning_ready"

# Wording approved by the owner (S06, 08.10.2026); display conditions follow TA.
HEADINGS: dict[Language, str] = {
    "ru": "Напоминание об изучении.",
    "uk": "Нагадування про вивчення.",
    "en": "Learning reminder.",
}
# Wording approved by the owner (S06, 08.10.2026); display conditions follow TA.
LEARNING_READY_LINES: dict[Language, str] = {
    "ru": "Готово карточек: {ready}",
    "uk": "Готово карток: {ready}",
    "en": "Cards ready: {ready}",
}
# Wording approved by the owner (S04, 08.10.2026); display conditions follow TA.
APP_BUTTON_LABELS: dict[Language, str] = {
    "ru": "Открыть приложение",
    "uk": "Відкрити застосунок",
    "en": "Open app",
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
    return OutgoingMessage("\n".join(lines), app_button=True, app_label=APP_BUTTON_LABELS[language])


TEMPLATE = Template(KIND, render, examples=(
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": LEARNING_READY, "ready": 3}]},
))
