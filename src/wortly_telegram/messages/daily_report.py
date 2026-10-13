"""Template of the daily_report job: the old bot's ``#daily`` retention report (Q06; К19-011; CON:315, TZC:470).

The core's builder (DEV-15) owns everything but the text: the previous UTC day, the first
touch of each card in the profile's directions, the 03:00 UTC window, five profiles per pass,
the switch, the admitted recipients (the owner too), the dedup by UTC date and the capacity
check (C05). This module only turns facts that are already fixed into one message.

The body is flat and has exactly three facts, none of them personal:

    utc_date  the reported UTC day, "YYYY-MM-DD" (it may differ from the Mini App's local day)
    total     cards touched that day, each once by its first answer
    correct   of them answered right

The percent is not a fact: it is ``round(correct / total * 100, 1)`` as in the old bot
(``reports.py``, ``storage.get_daily_first_touch_retention``), and the zone follows that
rounded value: below 70, 70 to below 80, 80 to 90 inclusive, above 90 to 95 inclusive, above 95.
A day without a first touch is no message (CON:315): the template fails closed, and so do
facts that contradict each other.

The text is a monospace Markdown block, sent with ``parse_mode="MarkdownV2"`` as the old bot
sent it with ``Markdown`` (an engineering reading of TZC:470, the only exception to ВП-16 apart
from the common-field echo; where the exception lives is the owner's open root decision).
MarkdownV2 is chosen because inside a ``pre`` block it needs only "`" and "\\" escaped, which
legacy Markdown cannot do at all. Every placed value is escaped, so no fact can end the block.
The message is ordinary (not quiet), has no button, no name, no chat and no pair.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import re
from typing import Any

from . import Language, OutgoingMessage, RenderError, Template


KIND = "daily_report"

_FIELDS = frozenset({"utc_date", "total", "correct"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# WORKING TEXT (not set by the specification): the lines of #daily are the old bot's wording
# (german_dictionary bot/i18n.py report_retention_daily, report_zone_*), with "UTC" after the
# date as TZC:470 requires; the wording for Wortly is not shown to the owner yet (Q06 keeps
# the report, not its words); final wording waits for the owner (T-TEXTS-PAGE).
DAILY_TEXTS: dict[Language, dict[str, str]] = {
    "ru": {
        "title": "Ретеншн ({day} UTC, первое касание): {rate}%",
        "correct": "Верно: {correct}/{total}",
        "overload": "<70%: перегрузка — слишком трудно, сбавь",
        "below_optimal": "70-80%: ниже оптимума — трудновато",
        "optimal": "80-90%: оптимально",
        "slightly_easy": "90-95%: легковато — можно добавить слов",
        "too_easy": ">95%: слишком легко — добавь новых слов",
    },
    "uk": {
        "title": "Ретеншн ({day} UTC, перший дотик): {rate}%",
        "correct": "Вірно: {correct}/{total}",
        "overload": "<70%: перевантаження — надто важко, збав темп",
        "below_optimal": "70-80%: нижче оптимуму — важкувато",
        "optimal": "80-90%: оптимально",
        "slightly_easy": "90-95%: трохи легко — можна додати слів",
        "too_easy": ">95%: надто легко — додай нових слів",
    },
    "en": {
        "title": "Retention ({day} UTC, first touch): {rate}%",
        "correct": "Correct: {correct}/{total}",
        "overload": "<70%: overload — too hard, ease off",
        "below_optimal": "70-80%: below optimal — a bit hard",
        "optimal": "80-90%: optimal",
        "slightly_easy": "90-95%: slightly easy — you can add words",
        "too_easy": ">95%: too easy — add more new words",
    },
}

HASHTAG = "#daily"


def escape_pre(text: str) -> str:
    """Escape ``text`` for a MarkdownV2 ``pre`` block: only "\\" and "`" (Bot API, MarkdownV2 style)."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise RenderError("A daily count is a non-negative integer")
    return value


def _utc_date(value: object) -> date:
    if type(value) is not str or not _DATE.fullmatch(value):
        raise RenderError("The reported UTC day is a date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RenderError("The reported UTC day is a date") from None


def rate_of(correct: int, total: int) -> float:
    """The old bot's rate: percent of right first touches, one decimal."""
    return round((correct / total) * 100, 1)


def zone_of(rate: float) -> str:
    """The zone key of a rounded rate: <70, 70-<80, 80-90 incl., >90-95 incl., >95 (as the old bot)."""
    if rate < 70.0:
        return "overload"
    if rate < 80.0:
        return "below_optimal"
    if rate <= 90.0:
        return "optimal"
    return "slightly_easy" if rate <= 95.0 else "too_easy"


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    if set(body) != _FIELDS:
        raise RenderError("The daily_report body has unexpected facts")
    day = _utc_date(body["utc_date"])
    total, correct = _count(body["total"]), _count(body["correct"])
    if total == 0:
        # A day without a first touch is no message (CON:315); the core must not ask for one.
        raise RenderError("A day without a first touch has no report")
    if correct > total:
        raise RenderError("More right cards than cards")
    texts = DAILY_TEXTS[language]
    rate = rate_of(correct, total)
    lines = [HASHTAG,
             texts["title"].format(day=day.isoformat(), rate=f"{rate:.1f}"),
             texts["correct"].format(correct=correct, total=total),
             texts[zone_of(rate)]]
    block = "\n".join(escape_pre(line) for line in lines)
    return OutgoingMessage(f"```text\n{block}\n```", parse_mode="MarkdownV2")


TEMPLATE = Template(KIND, render, examples=(
    {"utc_date": "2026-10-12", "total": 20, "correct": 17},
    {"utc_date": "2026-10-12", "total": 1, "correct": 1},
))
