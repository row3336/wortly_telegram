"""Template of the access_approved job: the login button after an approval (К02-028; T-D §4.3).

The core's builder (wortly.jobs.builders.access_approved) sends an empty body; the
message is the notice and the Mini App button. The texts are the old bot's approval
notice (OLD bot/i18n.py "access_approved_notice", R-TG 2.08), which names the button
"Start"; final wording waits for the owner (Q20).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Language, OutgoingMessage, RenderError, Template


KIND = "access_approved"

TEXTS: dict[Language, str] = {
    "ru": "✅ Доступ подтверждён! Нажмите «Старт».",
    "uk": "✅ Доступ підтверджено! Натисніть «Старт».",
    "en": "✅ Access granted! Press Start.",
}


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    if body:
        raise RenderError("The access_approved body carries no facts")
    return OutgoingMessage(TEXTS[language], app_button=True)


TEMPLATE = Template(KIND, render, examples=({},))
