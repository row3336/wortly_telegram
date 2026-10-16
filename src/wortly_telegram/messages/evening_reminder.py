"""Template of the evening_reminder job: the evening reminder of ready reviews (project_notifications ed.4 §7.3, T1).

The core's builder (wortly.jobs.builders.evening_reminder, N3) sends the same body as the
study reminder: the local date, the active pair and the ready items, each with a count.
The evening item is "reviews_ready"; "learning_ready" comes first when a Learning reminder
is due at the same moment (К12-019). The text is the one of study_reminder (table 6: the
heading "Напоминание об изучении." and the Mini App button, S06/S04; lines of the items in
the order Learning, reviews), so this module only gives the render its own job kind.

A release of the core that writes this kind goes out together with this template (N-AC33).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import Language, OutgoingMessage, Template
from .study_reminder import LEARNING_READY, REVIEWS_READY, render_reminder


KIND = "evening_reminder"


def render(body: Mapping[str, Any], language: Language) -> OutgoingMessage:
    return render_reminder(body, language, KIND)


TEMPLATE = Template(KIND, render, examples=(
    {"local_date": "2026-10-08", "pair": "ru-de", "items": [{"type": REVIEWS_READY, "ready": 4}]},
    {"local_date": "2026-10-08", "pair": "ru-de",
     "items": [{"type": LEARNING_READY, "ready": 3}, {"type": REVIEWS_READY, "ready": 4}]},
))
