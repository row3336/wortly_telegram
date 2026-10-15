"""Texts, buttons and views of the owner's common editor in the bot (project DEV-17 §3, §4.6; R12-05).

Nothing here calls the core or Telegram: the service function (service/card_editor.py) reads
and writes through the gateway and shows what this module renders. Every view is plain text
(ВП-16); only the copyable echo (echo.py) is MarkdownV2.

Buttons (§4.6). callback_data is ``ce:<action>:<arguments>`` and names no actor. A card is its
UUID in unpadded base64url (22 characters). A button that leads to a command carries
everything the command needs: the language, the card revision it was drawn from, the number
of the translation and the number of translations at that moment (§3.3), or the grammar field
and its value. The command's body is then a function of the update alone (§4.4), and the
buttons work after a restart of the bot (§3.6); stale data meets the core's VERSION_CONFLICT.
The longest button, ``ce:rm:<lang>:<card>:<revision>:<i>:<n>``, has 57 bytes for i, n <= 99;
a list of more than 99 translations of one language gets no edit buttons at all.

Owner's answers of 15.10.2026: R17-01 variant 1 (no unpublished cards in the bot: no
"unpublished" state or column here) and R17-02 variant 1 (every language's translations are
editable; the other languages than the dictionary language stay folded until the owner opens
them with their button). The grammar fields per type are the core's GRAMMAR_FIELDS (R12-05);
a test compares the copy below with the core's.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import re
from uuid import UUID

from .gateway import (
    INDEX_MAX, OWNER_LANGUAGES, GrammarForm, OwnerCard, OwnerSearchCard, is_version,
)
from .messages import CallbackButton, Language


CALLBACK_PREFIX = "ce"
# Telegram's bound of one message's text (Bot API sendMessage), in characters.
TEXT_MAX_CHARACTERS = 4096
# The core's GRAMMAR_FIELDS (dictionary/common_fields.py, R12-05): the bot cannot import that
# module (it needs the database driver), so it keeps this copy and a test compares the two.
GRAMMAR_FIELDS: dict[str, tuple[str, ...]] = {
    "noun": ("gender", "singular", "plural", "article"),
    "verb": ("infinitive", "preterite", "participle_ii", "auxiliary", "separability"),
    "adjective": ("positive", "comparative", "superlative"),
}
# Fields whose form is a free text the owner types (the echo is shown before the input).
TEXT_FIELDS = frozenset({"singular", "plural", "infinitive", "preterite", "participle_ii", "positive",
                         "comparative", "superlative"})

# -- texts -------------------------------------------------------------------------------

# WORKING TEXT (not set by the specification): the Russian answers are the working texts of
# the project DEV-17 ed. 3.3 §3.7 (shown to the owner there, not approved); the Ukrainian and
# English ones are the worker's translations; final wording waits for the owner (T-TEXTS-PAGE).
ANSWERS: dict[Language, dict[str, str]] = {
    "ru": {
        "saved": "✅ Сохранено.",
        "conflict": "Карточку уже изменили (возможно, в Mini App). Показываю актуальные данные — повтори действие.",
        "last_translation": ("Нельзя убрать последний общий перевод языка: без него карточка пропадёт из словаря "
                             "у всех. Сначала добавь другой перевод. Убрать карточку можно только снятием "
                             "с публикации."),
        "validation": ("Ядро не приняло значение: карточка стала бы неполной или письменный вопрос по ней — "
                       "невозможным. Ничего не изменено."),
        "forbidden": "Это действие доступно только владельцу.",
        "card_unavailable": "Карточка недоступна. Обнови поиск: /find",
        "core_silent": "Ядро не ответило. Изменение могло сохраниться — нажми «Обновить», прежде чем повторять.",
        "translation_prompt": "Пришли новый текст перевода №{index}.",
        "append_prompt": "Пришли новый перевод одной строкой.",
        "one_line": "Пришли один перевод одной строкой.",
    },
    "uk": {
        "saved": "✅ Збережено.",
        "conflict": "Картку вже змінили (можливо, в Mini App). Показую актуальні дані — повтори дію.",
        "last_translation": ("Не можна прибрати останній спільний переклад мови: без нього картка зникне зі "
                             "словника в усіх. Спочатку додай інший переклад. Прибрати картку можна лише "
                             "зняттям з публікації."),
        "validation": ("Ядро не прийняло значення: картка стала б неповною або письмове питання по ній — "
                       "неможливим. Нічого не змінено."),
        "forbidden": "Ця дія доступна лише власнику.",
        "card_unavailable": "Картка недоступна. Онови пошук: /find",
        "core_silent": "Ядро не відповіло. Зміна могла зберегтися — натисни «Оновити», перш ніж повторювати.",
        "translation_prompt": "Надішли новий текст перекладу №{index}.",
        "append_prompt": "Надішли новий переклад одним рядком.",
        "one_line": "Надішли один переклад одним рядком.",
    },
    "en": {
        "saved": "✅ Saved.",
        "conflict": "The card has already been changed (perhaps in the Mini App). Showing the current data — "
                    "repeat the action.",
        "last_translation": ("The last common translation of a language cannot be removed: without it the card "
                             "disappears from everyone's dictionary. Add another translation first. A card can "
                             "only be removed by unpublishing it."),
        "validation": ("The core did not accept the value: the card would become incomplete or its written "
                       "question impossible. Nothing was changed."),
        "forbidden": "Only the owner can do this.",
        "card_unavailable": "The card is unavailable. Search again: /find",
        "core_silent": "The core did not answer. The change may have been saved — press “Refresh” before "
                       "repeating it.",
        "translation_prompt": "Send the new text of translation #{index}.",
        "append_prompt": "Send the new translation in one line.",
        "one_line": "Send one translation in one line.",
    },
}

# WORKING TEXT (not set by the specification): the editor's other notices, new (the old bot had
# no such search; "Не нашёл…" and "Показаны первые 10…" follow the project §3.1); final wording
# waits for the owner (T-TEXTS-PAGE).
NOTICES: dict[Language, dict[str, str]] = {
    "ru": {
        "search_prompt": "Пришли слово или номер карточки.",
        "not_found": "Не нашёл. Пришли другое слово или номер.",
        "found": "Найдено карточек: {count}.",
        "first_ten": "Показаны первые 10, уточни запрос.",
        "query_too_long": "Запрос длиннее 200 символов. Пришли слово короче.",
        "read_failed": "Ядро не ответило. Попробуй ещё раз.",
        "form_prompt": "Пришли форму «{field}» одной строкой.",
        "form_one_line": "Пришли форму одной строкой.",
        "too_many": "Переводов больше 99: правка этого списка в боте недоступна.",
        "stale_button": "Эта кнопка устарела. Показываю актуальные данные — повтори действие.",
    },
    "uk": {
        "search_prompt": "Надішли слово або номер картки.",
        "not_found": "Не знайшов. Надішли інше слово або номер.",
        "found": "Знайдено карток: {count}.",
        "first_ten": "Показано перші 10, уточни запит.",
        "query_too_long": "Запит довший за 200 символів. Надішли коротше слово.",
        "read_failed": "Ядро не відповіло. Спробуй ще раз.",
        "form_prompt": "Надішли форму «{field}» одним рядком.",
        "form_one_line": "Надішли форму одним рядком.",
        "too_many": "Перекладів понад 99: редагування цього списку в боті недоступне.",
        "stale_button": "Ця кнопка застаріла. Показую актуальні дані — повтори дію.",
    },
    "en": {
        "search_prompt": "Send a word or a card number.",
        "not_found": "Nothing found. Send another word or number.",
        "found": "Cards found: {count}.",
        "first_ten": "Showing the first 10, refine the query.",
        "query_too_long": "The query is longer than 200 characters. Send a shorter word.",
        "read_failed": "The core did not answer. Try again.",
        "form_prompt": "Send the form “{field}” in one line.",
        "form_one_line": "Send the form in one line.",
        "too_many": "More than 99 translations: this list cannot be edited in the bot.",
        "stale_button": "This button is out of date. Showing the current data — repeat the action.",
    },
}

# WORKING TEXT (not set by the specification): the lines of the card, list and grammar views
# (project §3.2 after ed. 1); final wording waits for the owner (T-TEXTS-PAGE).
VIEW_TEXTS: dict[Language, dict[str, str]] = {
    "ru": {
        "visible": "Состояние: опубликована",
        "no_translation": "Состояние: скрыта — нет общего перевода ({language})",
        "own": "Переводы {language} (язык словаря): {texts}",
        "folded": "Другие языки свёрнуты: {languages}",
        "grammar": "Грамматика: {forms}",
        "no_grammar": "Грамматика: не заполнена",
        "synonyms": "Синонимы: {members}",
        "list": "{lemma} — переводы {language}:",
        "empty": "Переводов нет.",
        "grammar_menu": "{lemma} — грамматика:",
        "field": "{lemma} — {field}: {value}. Выбери:",
        "none": "—",
        "filled": "применимо, без формы",
        "not_applicable": "не применимо",
        "unknown": "неизвестно",
    },
    "uk": {
        "visible": "Стан: опублікована",
        "no_translation": "Стан: прихована — немає спільного перекладу ({language})",
        "own": "Переклади {language} (мова словника): {texts}",
        "folded": "Інші мови згорнуті: {languages}",
        "grammar": "Граматика: {forms}",
        "no_grammar": "Граматика: не заповнена",
        "synonyms": "Синоніми: {members}",
        "list": "{lemma} — переклади {language}:",
        "empty": "Перекладів немає.",
        "grammar_menu": "{lemma} — граматика:",
        "field": "{lemma} — {field}: {value}. Обери:",
        "none": "—",
        "filled": "застосовно, без форми",
        "not_applicable": "не застосовно",
        "unknown": "невідомо",
    },
    "en": {
        "visible": "State: published",
        "no_translation": "State: hidden — no common translation ({language})",
        "own": "Translations {language} (dictionary language): {texts}",
        "folded": "Other languages folded: {languages}",
        "grammar": "Grammar: {forms}",
        "no_grammar": "Grammar: not filled",
        "synonyms": "Synonyms: {members}",
        "list": "{lemma} — translations {language}:",
        "empty": "No translations.",
        "grammar_menu": "{lemma} — grammar:",
        "field": "{lemma} — {field}: {value}. Choose:",
        "none": "—",
        "filled": "applicable, no form",
        "not_applicable": "not applicable",
        "unknown": "unknown",
    },
}

# WORKING TEXT (not set by the specification): the editor's button labels (project §3.2-§3.4);
# final wording waits for the owner (T-TEXTS-PAGE).
LABELS: dict[Language, dict[str, str]] = {
    "ru": {"own": "✏️ Переводы {language}", "other": "▸ {language} ({count})", "grammar": "Грамматика",
           "synonyms": "Синонимы",
           "refresh": "🔄 Обновить", "back": "⬅ К карточке", "add": "➕ Добавить", "edit": "✏️ {index}",
           "up": "⬆", "down": "⬇", "remove": "✖", "enter": "Ввести форму", "to_grammar": "⬅ Грамматика"},
    "uk": {"own": "✏️ Переклади {language}", "other": "▸ {language} ({count})", "grammar": "Граматика",
           "synonyms": "Синоніми",
           "refresh": "🔄 Оновити", "back": "⬅ До картки", "add": "➕ Додати", "edit": "✏️ {index}",
           "up": "⬆", "down": "⬇", "remove": "✖", "enter": "Ввести форму", "to_grammar": "⬅ Граматика"},
    "en": {"own": "✏️ Translations {language}", "other": "▸ {language} ({count})", "grammar": "Grammar",
           "synonyms": "Synonyms",
           "refresh": "🔄 Refresh", "back": "⬅ To the card", "add": "➕ Add", "edit": "✏️ {index}",
           "up": "⬆", "down": "⬇", "remove": "✖", "enter": "Enter a form", "to_grammar": "⬅ Grammar"},
}

# WORKING TEXT (not set by the specification): short names of the card types (project §3.1,
# "сущ."); final wording waits for the owner (T-TEXTS-PAGE).
TYPE_LABELS: dict[Language, dict[str, str]] = {
    "ru": {"noun": "сущ.", "verb": "гл.", "phrase": "фраза", "adjective": "прил.", "pronoun": "мест.",
           "adverb": "нареч.", "preposition": "предл.", "conjunction": "союз", "particle": "частица"},
    "uk": {"noun": "ім.", "verb": "дієсл.", "phrase": "фраза", "adjective": "прикм.", "pronoun": "займ.",
           "adverb": "присл.", "preposition": "прийм.", "conjunction": "спол.", "particle": "частка"},
    "en": {"noun": "noun", "verb": "verb", "phrase": "phrase", "adjective": "adj.", "pronoun": "pron.",
           "adverb": "adv.", "preposition": "prep.", "conjunction": "conj.", "particle": "particle"},
}

# WORKING TEXT (not set by the specification): names of the grammar fields of R12-05 and the
# "hidden" mark of a search result (project §3.1, §3.4); final wording waits for the owner
# (T-TEXTS-PAGE).
FIELD_LABELS: dict[Language, dict[str, str]] = {
    "ru": {"gender": "род", "singular": "ед. ч.", "plural": "мн. ч.", "article": "артикль",
           "infinitive": "инфинитив", "preterite": "претерит", "participle_ii": "причастие II",
           "auxiliary": "вспом. глагол", "separability": "отделяемость", "positive": "положит. степень",
           "comparative": "сравнит. степень", "superlative": "превосх. степень", "hidden": "скрыта"},
    "uk": {"gender": "рід", "singular": "одн.", "plural": "мн.", "article": "артикль",
           "infinitive": "інфінітив", "preterite": "претерит", "participle_ii": "дієприкметник II",
           "auxiliary": "доп. дієслово", "separability": "відокремлюваність", "positive": "звич. ступінь",
           "comparative": "вищий ступінь", "superlative": "найвищий ступінь", "hidden": "прихована"},
    "en": {"gender": "gender", "singular": "singular", "plural": "plural", "article": "article",
           "infinitive": "infinitive", "preterite": "preterite", "participle_ii": "participle II",
           "auxiliary": "auxiliary", "separability": "separability", "positive": "positive",
           "comparative": "comparative", "superlative": "superlative", "hidden": "hidden"},
}

# WORKING TEXT (not set by the specification): the labels of the grammar values (project §3.4:
# "[Ввести форму] [Не применимо] [Неизвестно]", "[der] [die] [das]", "[Применим]", "[отделяемый]");
# final wording waits for the owner (T-TEXTS-PAGE).
VALUE_LABELS: dict[Language, dict[str, str]] = {
    "ru": {"na": "Не применимо", "un": "Неизвестно", "m": "der", "f": "die", "n": "das", "ap": "Применим",
           "art_na": "Не применим", "h": "haben", "s": "sein", "sp": "отделяемый", "is": "неотделяемый",
           "sep_un": "неизвестно"},
    "uk": {"na": "Не застосовно", "un": "Невідомо", "m": "der", "f": "die", "n": "das", "ap": "Застосовний",
           "art_na": "Не застосовний", "h": "haben", "s": "sein", "sp": "відокремлюваний",
           "is": "невідокремлюваний", "sep_un": "невідомо"},
    "en": {"na": "Not applicable", "un": "Unknown", "m": "der", "f": "die", "n": "das", "ap": "Applicable",
           "art_na": "Not applicable", "h": "haben", "s": "sein", "sp": "separable", "is": "inseparable",
           "sep_un": "unknown"},
}


# -- button data ---------------------------------------------------------------------------

OPEN, CARD, LIST, EDIT, UP, DOWN, REMOVE, ADD = "o", "c", "tl", "ed", "up", "dn", "rm", "ad"
GRAMMAR, FIELD, VALUE, ENTER = "g", "gf", "gv", "gi"
LIST_COMMANDS = {UP: "move_up", DOWN: "move_down", REMOVE: "remove"}

# Field and value codes of the grammar buttons; the wire value each value code stands for.
FIELD_CODES: dict[str, str] = {
    "ge": "gender", "sg": "singular", "pl": "plural", "ar": "article", "if": "infinitive", "pt": "preterite",
    "p2": "participle_ii", "ax": "auxiliary", "sp": "separability", "po": "positive", "cp": "comparative",
    "su": "superlative",
}
CODE_OF_FIELD = {name: code for code, name in FIELD_CODES.items()}
_FORM_VALUES = {"na": {"status": "not_applicable", "value": None}, "un": {"status": "unknown", "value": None}}
FIELD_VALUES: dict[str, dict[str, dict | str]] = {
    **{name: dict(_FORM_VALUES) for name in TEXT_FIELDS},
    "gender": {"m": {"status": "applicable", "value": "masculine"},
               "f": {"status": "applicable", "value": "feminine"},
               "n": {"status": "applicable", "value": "neuter"}, **_FORM_VALUES},
    "article": {"ap": {"status": "applicable", "value": None}, **_FORM_VALUES},
    "auxiliary": {"h": {"status": "applicable", "value": "haben"}, "s": {"status": "applicable", "value": "sein"},
                  **_FORM_VALUES},
    "separability": {"sp": "separable", "is": "inseparable", "un": "unknown"},
}

_CARD = re.compile(r"[A-Za-z0-9_-]{22}")
_COUNT = re.compile(r"[1-9][0-9]?")
# The number of translations under "Add" may be 0: a hidden card has none in its language yet.
_ADD_COUNT = re.compile(r"0|[1-9][0-9]?")


def card_code(card_id: UUID) -> str:
    """The card's UUID as 22 characters of unpadded base64url."""
    return base64.urlsafe_b64encode(card_id.bytes).decode("ascii").rstrip("=")


def card_of(code: str) -> UUID | None:
    """The UUID of a card code, or None for anything that is not one in its canonical form."""
    if not _CARD.fullmatch(code):
        return None
    card_id = UUID(bytes=base64.urlsafe_b64decode(code + "=="))
    return card_id if card_code(card_id) == code else None


@dataclass(frozen=True)
class Press:
    """One parsed button of the editor: its action and the references it carries."""

    action: str
    card_id: UUID
    language: str | None = None
    revision: int | None = None
    index: int | None = None
    count: int | None = None
    field_name: str | None = None
    value: dict | str | None = None


def _count(value: str, pattern: re.Pattern = _COUNT) -> int | None:
    return int(value) if pattern.fullmatch(value) else None


def parse(data: str | None) -> Press | None:
    """The press of an editor button, or None for anything malformed or foreign."""
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action, arguments = parts[1], parts[2:]
    if action in (OPEN, CARD, GRAMMAR):
        card_id = card_of(arguments[0]) if len(arguments) == 1 else None
        return None if card_id is None else Press(action, card_id)
    if action == LIST:
        if len(arguments) != 2 or arguments[0] not in OWNER_LANGUAGES:
            return None
        card_id = card_of(arguments[1])
        return None if card_id is None else Press(action, card_id, language=arguments[0])
    if action in (EDIT, UP, DOWN, REMOVE, ADD):
        size = 4 if action == ADD else 5
        if len(arguments) != size or arguments[0] not in OWNER_LANGUAGES or not is_version(arguments[2]):
            return None
        pattern = _ADD_COUNT if action == ADD else _COUNT
        card_id, counts = card_of(arguments[1]), [_count(value, pattern) for value in arguments[3:]]
        if card_id is None or None in counts:
            return None
        index, count = (None, counts[0]) if action == ADD else counts
        if index is not None and index > count:
            return None
        return Press(action, card_id, language=arguments[0], revision=int(arguments[2]), index=index, count=count)
    if action in (FIELD, ENTER, VALUE):
        size = 4 if action == VALUE else 3
        if len(arguments) != size or not is_version(arguments[1]) or arguments[2] not in FIELD_CODES:
            return None
        card_id, field_name = card_of(arguments[0]), FIELD_CODES[arguments[2]]
        if card_id is None or (action == ENTER and field_name not in TEXT_FIELDS):
            return None
        value = None
        if action == VALUE:
            value = FIELD_VALUES[field_name].get(arguments[3])
            if value is None:
                return None
        return Press(action, card_id, revision=int(arguments[1]), field_name=field_name, value=value)
    return None


def _data(*parts: object) -> str:
    return ":".join((CALLBACK_PREFIX, *(str(part) for part in parts)))


# -- views ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class View:
    """A rendered message of the editor: plain text and rows of callback buttons."""

    text: str
    buttons: tuple[tuple[CallbackButton, ...], ...] = ()


def fit(text: str) -> str:
    """``text`` within Telegram's bound of a message; a longer one ends with "…"."""
    return text if len(text) <= TEXT_MAX_CHARACTERS else text[:TEXT_MAX_CHARACTERS - 1] + "…"


def with_notice(notice: str, view: View) -> View:
    return View(fit(f"{notice}\n\n{view.text}"), view.buttons)


def search_label(card: OwnerSearchCard, language: Language) -> str:
    """``das Haus · сущ. · A1`` with ``· скрыта`` for a card hidden for lack of a translation (§3.1)."""
    parts = [card.lemma, TYPE_LABELS[language][card.card_type]]
    if card.cefr is not None:
        parts.append(card.cefr)
    if card.state == "no_translation":
        parts.append(FIELD_LABELS[language]["hidden"])
    return " · ".join(parts)


def search_view(cards: tuple[OwnerSearchCard, ...], more: bool, language: Language) -> View:
    notices = NOTICES[language]
    text = notices["first_ten"] if more else notices["found"].format(count=len(cards))
    return View(text, tuple((CallbackButton(search_label(card, language), _data(OPEN, card_code(card.card_id))),)
                            for card in cards))


def _heading(card: OwnerCard, language: Language) -> str:
    parts = [TYPE_LABELS[language][card.card_type]] + ([card.cefr] if card.cefr else [])
    return f"{card.lemma} — {', '.join(parts)}"


def form_text(card: OwnerCard, field_name: str, language: Language) -> str:
    """How one grammar field of the card reads in a view."""
    texts, values = VIEW_TEXTS[language], VALUE_LABELS[language]
    if card.grammar is None:
        return texts["none"]
    value = card.grammar[field_name]
    if isinstance(value, str):
        return values[{"separable": "sp", "inseparable": "is", "unknown": "sep_un"}[value]]
    if value.status != "applicable":
        return texts[value.status]
    if field_name == "article":
        return values["ap"]
    if value.value is None:
        return texts["filled"]
    if field_name == "gender":
        return values[{"masculine": "m", "feminine": "f", "neuter": "n"}[value.value]]
    return value.value


def card_view(card: OwnerCard, language: Language) -> View:
    """The card (§3.2): state, the dictionary language's translations, the other languages folded, grammar."""
    texts, labels = VIEW_TEXTS[language], LABELS[language]
    own = card.translations[card.language]
    lines = [_heading(card, language),
             texts[card.state].format(language=card.language),
             texts["own"].format(language=card.language,
                                  texts="; ".join(f"{number}. {text}" for number, text in enumerate(own, 1))
                                  or texts["none"])]
    others = [other for other in OWNER_LANGUAGES if other != card.language]
    lines.append(texts["folded"].format(languages=", ".join(
        f"{other} ({len(card.translations[other])})" for other in others)))
    code = card_code(card.card_id)
    rows = [(CallbackButton(labels["own"].format(language=card.language), _data(LIST, card.language, code)),),
            tuple(CallbackButton(labels["other"].format(language=other, count=len(card.translations[other])),
                                 _data(LIST, other, code)) for other in others)]
    if card.card_type in GRAMMAR_FIELDS:
        fields = GRAMMAR_FIELDS[card.card_type]
        lines.append(texts["grammar"].format(forms="; ".join(
            f"{FIELD_LABELS[language][name]} — {form_text(card, name, language)}" for name in fields))
            if card.grammar is not None else texts["no_grammar"])
        rows.append((CallbackButton(labels["grammar"], _data(GRAMMAR, code)),))
    # The synonym group (§3.5, stage B4): the other members' lemmas; its own function answers "sy:".
    others = [member.lemma for member in card.synonyms.members if member.card_id != card.card_id]
    lines.append(texts["synonyms"].format(members=", ".join(others) or texts["none"]))
    rows.append((CallbackButton(labels["synonyms"], f"sy:v:{code}"),))
    rows.append((CallbackButton(labels["refresh"], _data(CARD, code)),))
    return View(fit("\n".join(lines)), tuple(rows))


def list_view(card: OwnerCard, list_language: str, language: Language) -> View:
    """The translations of one language (§3.3): ``[✏️ i] [⬆] [⬇] [✖]`` per translation, ``[➕] [⬅]`` below.

    Every button carries the language, the revision, i and n of this moment. Over INDEX_MAX
    translations no edit button is drawn (§4.6): the list is shown only.
    """
    texts, labels, code = VIEW_TEXTS[language], LABELS[language], card_code(card.card_id)
    translations = card.translations[list_language]
    count, revision = len(translations), card.revision
    lines = [texts["list"].format(lemma=card.lemma, language=list_language)]
    lines += [f"{number}. {text}" for number, text in enumerate(translations, 1)] or [texts["empty"]]
    rows: list[tuple[CallbackButton, ...]] = []
    back = CallbackButton(labels["back"], _data(CARD, code))
    if count > INDEX_MAX:
        lines.append(NOTICES[language]["too_many"])
        return View(fit("\n".join(lines)), ((back,),))
    for index in range(1, count + 1):
        def button(action: str, label: str) -> CallbackButton:
            return CallbackButton(label, _data(action, list_language, code, revision, index, count))
        row = [button(EDIT, labels["edit"].format(index=index))]
        if index > 1:
            row.append(button(UP, labels["up"]))
        if index < count:
            row.append(button(DOWN, labels["down"]))
        row.append(button(REMOVE, labels["remove"]))
        rows.append(tuple(row))
    rows.append((CallbackButton(labels["add"], _data(ADD, list_language, code, revision, count)), back))
    return View(fit("\n".join(lines)), tuple(rows))


def grammar_view(card: OwnerCard, language: Language) -> View:
    """The grammar fields of the card's type (R12-05), one button each, with their current values."""
    texts, labels, code = VIEW_TEXTS[language], LABELS[language], card_code(card.card_id)
    fields = GRAMMAR_FIELDS.get(card.card_type, ())
    lines = [texts["grammar_menu"].format(lemma=card.lemma)]
    lines += [f"{FIELD_LABELS[language][name]}: {form_text(card, name, language)}" for name in fields]
    buttons = [CallbackButton(FIELD_LABELS[language][name], _data(FIELD, code, card.revision, CODE_OF_FIELD[name]))
               for name in fields]
    rows = [tuple(buttons[start:start + 2]) for start in range(0, len(buttons), 2)]
    rows.append((CallbackButton(labels["back"], _data(CARD, code)),))
    return View(fit("\n".join(lines)), tuple(rows))


def field_options(card: OwnerCard, field_name: str) -> tuple[str, ...]:
    """The value codes the core can accept for this field of this published card (§3.4).

    The core still decides; these are the buttons only. Published cards only (R17-01 variant 1).
    """
    if field_name in ("singular", "plural", "infinitive", "positive"):
        return ("enter", "na", "un")
    if field_name in ("preterite", "comparative", "superlative", "participle_ii"):
        # unknown is refused on a published card (validation.py :63-64, :312-317, :324-326).
        return ("enter", "na")
    if field_name == "gender":
        return ("m", "f", "n", "na", "un")
    if field_name == "article":
        return ("ap", "na", "un")
    if field_name == "separability":
        return ("sp", "is", "un")
    if field_name == "auxiliary":
        participle = card.grammar.get("participle_ii") if card.grammar is not None else None
        applicable = isinstance(participle, GrammarForm) and participle.status == "applicable"
        # With an applicable Partizip II the auxiliary is haben or sein (validation.py :320-328).
        return ("h", "s") if applicable else ("h", "s", "na", "un")
    raise ValueError("Not a grammar field")


def field_view(card: OwnerCard, field_name: str, language: Language) -> View:
    """The choice of one grammar field's value; "Enter a form" for the free-text fields."""
    texts, labels, values = VIEW_TEXTS[language], LABELS[language], VALUE_LABELS[language]
    code, field_code = card_code(card.card_id), CODE_OF_FIELD[field_name]
    buttons = []
    for option in field_options(card, field_name):
        if option == "enter":
            buttons.append(CallbackButton(labels["enter"], _data(ENTER, code, card.revision, field_code)))
            continue
        label = values[{("article", "na"): "art_na", ("separability", "un"): "sep_un"}.get((field_name, option),
                                                                                          option)]
        buttons.append(CallbackButton(label, _data(VALUE, code, card.revision, field_code, option)))
    rows = [tuple(buttons[start:start + 3]) for start in range(0, len(buttons), 3)]
    rows.append((CallbackButton(labels["to_grammar"], _data(GRAMMAR, code)),))
    text = texts["field"].format(lemma=card.lemma, field=FIELD_LABELS[language][field_name],
                                 value=form_text(card, field_name, language))
    return View(fit(text), tuple(rows))


def current_form(card: OwnerCard, field_name: str) -> str | None:
    """The free-text form of a field to echo before its input, or None when it has none."""
    if card.grammar is None or field_name not in TEXT_FIELDS:
        return None
    value = card.grammar[field_name]
    return value.value if isinstance(value, GrammarForm) else None
