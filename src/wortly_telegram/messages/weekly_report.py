"""Template of the weekly_report job: the quiet report of the written quiz for the last local week (Q05; К12-008…012).

The core's builder (DEV-15) owns everything but the text: it picks the Monday 07:00 of the
profile's zone, computes the previous local week, checks the switch, deduplicates and
collects the facts. This module only turns facts that are already fixed into one message.

The body is flat and has exactly four facts, none of them personal:

    week_start       the Monday of the reported local week, "YYYY-MM-DD"
    study_actions    true when the week had any study action (the core's own notion)
    written_cards    cards of the written quiz, each once per week by its first answer (М77)
    written_correct  of them answered right; wrong is written_cards - written_correct

Only the written quiz counts (М79/М80) and the unit is the card. The template fails closed:
a week without study actions is never a message (К12-009), and so are facts that contradict
each other. A week with study actions but no written answer says 0 cards with a sentence and
has no percent. With cards, the share is correct / cards, one decimal, and the tempo advice
follows the share as shown: below 80 reduce new words, 80 to 90 inclusive keep the pace,
above 90 may raise. The report changes no setting.

The message is quiet (``silent=True``) and carries no button, no name, no chat and no pair.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
import re
from typing import Any

from . import Language, OutgoingMessage, RenderError, Template


KIND = "weekly_report"

_FIELDS = frozenset({"week_start", "study_actions", "written_cards", "written_correct"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Thresholds of the advice, in tenths of a percent as shown (М77): below 80,0 / up to 90,0 inclusive.
REDUCE_BELOW = 800
HOLD_UP_TO = 900

# WORKING TEXT (not set by the specification): the lines of the weekly report (М77 names the
# contents, the wording is not shown to the owner yet; Q05/G05); final wording waits for the
# owner (T-TEXTS-PAGE).
WEEKLY_TEXTS: dict[Language, dict[str, str]] = {
    "ru": {
        "title": "Недельный отчёт: {start} – {end}",
        "cards": "Карточек в письменных квизах: {cards}",
        "correct": "Верно: {correct}",
        "wrong": "Неверно: {wrong}",
        "share": "Доля верных: {share}%",
        "no_written": "На этой неделе были учебные действия, но письменных ответов не было.",
        "reduce": "Совет: уменьшить число новых слов.",
        "hold": "Совет: сохранить текущий темп.",
        "raise": "Совет: можно увеличить число новых слов.",
    },
    "uk": {
        "title": "Тижневий звіт: {start} – {end}",
        "cards": "Карток у письмових квізах: {cards}",
        "correct": "Вірно: {correct}",
        "wrong": "Невірно: {wrong}",
        "share": "Частка вірних: {share}%",
        "no_written": "Цього тижня були навчальні дії, але письмових відповідей не було.",
        "reduce": "Порада: зменшити кількість нових слів.",
        "hold": "Порада: зберегти поточний темп.",
        "raise": "Порада: можна збільшити кількість нових слів.",
    },
    "en": {
        "title": "Weekly report: {start} – {end}",
        "cards": "Written quiz cards: {cards}",
        "correct": "Correct: {correct}",
        "wrong": "Wrong: {wrong}",
        "share": "Share correct: {share}%",
        "no_written": "There were study actions this week, but no written answers.",
        "reduce": "Advice: reduce the number of new words.",
        "hold": "Advice: keep the current pace.",
        "raise": "Advice: you may increase the number of new words.",
    },
}


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise RenderError("A weekly count is a non-negative integer")
    return value


def _monday(value: object) -> date:
    if type(value) is not str or not _DATE.fullmatch(value):
        raise RenderError("The reported week starts on a date")
    try:
        start = date.fromisoformat(value)
    except ValueError:
        raise RenderError("The reported week starts on a date") from None
    if start.weekday() != 0:
        raise RenderError("The reported week starts on a Monday")
    return start


def share_tenths(correct: int, cards: int) -> int:
    """The share of right cards in tenths of a percent, rounded half up (М77: one decimal)."""
    return (correct * 2000 + cards) // (2 * cards)


def _share_text(tenths: int, language: Language) -> str:
    text = f"{tenths // 10}.{tenths % 10}"
    return text if language == "en" else text.replace(".", ",")


def _advice(tenths: int) -> str:
    if tenths < REDUCE_BELOW:
        return "reduce"
    return "hold" if tenths <= HOLD_UP_TO else "raise"


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    if set(body) != _FIELDS:
        raise RenderError("The weekly_report body has unexpected facts")
    start = _monday(body["week_start"])
    if type(body["study_actions"]) is not bool:
        raise RenderError("study_actions must be a boolean")
    cards, correct = _count(body["written_cards"]), _count(body["written_correct"])
    if not body["study_actions"]:
        # An empty week is no message (К12-009); cards without any action contradict the facts.
        raise RenderError("A week without study actions has no report")
    if correct > cards:
        raise RenderError("More right cards than cards")
    texts = WEEKLY_TEXTS[language]
    lines = [texts["title"].format(start=start.isoformat(), end=(start + timedelta(days=6)).isoformat()),
             texts["cards"].format(cards=cards)]
    if cards == 0:
        lines.append(texts["no_written"])
    else:
        tenths = share_tenths(correct, cards)
        lines += [texts["correct"].format(correct=correct), texts["wrong"].format(wrong=cards - correct),
                  texts["share"].format(share=_share_text(tenths, language)), texts[_advice(tenths)]]
    return OutgoingMessage("\n".join(lines), silent=True)


TEMPLATE = Template(KIND, render, examples=(
    {"week_start": "2026-10-05", "study_actions": True, "written_cards": 12, "written_correct": 9},
    {"week_start": "2026-10-05", "study_actions": True, "written_cards": 0, "written_correct": 0},
))
