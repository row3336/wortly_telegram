"""Forms of a counted noun for the three languages (design T-D §4.3 п.3).

Russian and Ukrainian distinguish one / few / many for whole numbers, English one /
other (the CLDR cardinal rules for integers). Templates (T4) pass every form their
language needs; a missing form is a template fault, raised before any network call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from . import LANGUAGES, Language, RenderError


Category = Literal["one", "few", "many", "other"]

FORMS: dict[Language, tuple[Category, ...]] = {
    "ru": ("one", "few", "many"),
    "uk": ("one", "few", "many"),
    "en": ("one", "other"),
}


def category(language: Language, count: int) -> Category:
    if language not in LANGUAGES:
        raise RenderError("Unsupported language for plural forms")
    if type(count) is not int or count < 0:
        raise RenderError("A count must be a non-negative integer")
    if language == "en":
        return "one" if count == 1 else "other"
    last, last_two = count % 10, count % 100
    if last == 1 and last_two != 11:
        return "one"
    if 2 <= last <= 4 and not 12 <= last_two <= 14:
        return "few"
    return "many"


def plural(language: Language, count: int, forms: Mapping[str, str]) -> str:
    """Return the form of ``forms`` for ``count`` in ``language``."""
    missing = [name for name in FORMS.get(language, ()) if name not in forms]
    if missing:
        raise RenderError("A plural form is missing")
    return forms[category(language, count)]
