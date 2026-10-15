"""Texts, buttons and views of the owner's synonym groups in the bot (project DEV-17 §3.5, §4.4, §4.6; B4).

Nothing here calls the core or Telegram (service/synonyms.py does). Plain text only (ВП-16).

Buttons. callback_data is ``sy:<action>:<arguments>`` and names no actor; a card is its
UUID in 22 characters of base64url (card_view.card_code). A button that needs only one
card and one group version carries them: ``sy:rm:<card>:<version>:<i>`` (51 bytes for
i <= 99), ``sy:cd`` and ``sy:cl`` with the version of the card's group as the view showed it
("0": no group; versions start at 1). ``sy:ad:<card>:<type>`` starts ➕ only: the versions of a
link are those at the moment of the search (§3.5), read when the owner's text arrives. The choice of a card to link and the
confirmations name two cards and two versions, or up to 20 cards, which do not fit 64
bytes: ``sy:pk:<key>`` and ``sy:ok:<key>`` carry an 8-character key of a state the service
keeps in memory when it shows the button (§3.5, §3.6). A command's body comes only from
the button's data or that state, never from a new read (§4.4).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import UUID

from .card_view import TYPE_LABELS, View, card_code, card_of, fit
from .gateway import INDEX_MAX, OwnerSearchCard, SynonymList, is_version
from .messages import CallbackButton, Language


CALLBACK_PREFIX = "sy"
VIEW, ADD, CANDIDATES, CLEAR, REMOVE, PICK, CONFIRM = "v", "ad", "cd", "cl", "rm", "pk", "ok"
# One letter per card type in the add button: the search for a synonym is limited to the type.
TYPE_CODES = {"noun": "n", "verb": "v", "phrase": "p", "adjective": "a", "pronoun": "r", "adverb": "d",
              "preposition": "e", "conjunction": "c", "particle": "t"}
TYPE_OF_CODE = {code: card_type for card_type, code in TYPE_CODES.items()}
_KEY = re.compile(r"[A-Za-z0-9_-]{8}")
_INDEX = re.compile(r"[1-9][0-9]?")
NO_GROUP = "0"

# WORKING TEXT (not set by the specification): the synonym editor's texts. "Подтверждение
# устарело. Повтори действие." is the project's working text of §3.6; the rest is new (the old
# bot's synonym screens, handlers/synonyms.py, had other wording); the Ukrainian and English
# ones are the worker's translations; final wording waits for the owner (T-TEXTS-PAGE).
SYNONYM_TEXTS: dict[Language, dict[str, str]] = {
    "ru": {
        "heading": "Синонимы ({card_type}):",
        "none": "Синонимов нет.",
        "this_card": "эта карточка",
        "add_prompt": "Пришли слово или номер карточки того же типа.",
        "not_found": "Не нашёл карточку того же типа. Пришли другое слово или номер.",
        "all_in_group": "Нашлась только эта карточка или карточки, уже связанные с ней. Пришли другое слово или номер.",
        "refine": "Показаны первые 10 найденных, уточни запрос.",
        "pick": "Выбери карточку, которую связать:",
        "candidates": "Подобрано по общему переводу в языке словаря. Связать все?",
        "candidates_more": "Показаны первые 20.",
        "no_candidates": "Карточек того же типа с таким же переводом в языке словаря нет.",
        "clear": "Очистить группу? Все карточки перестанут быть синонимами друг друга.",
        "stale": "Подтверждение устарело. Повтори действие.",
        "conflict": "Группу синонимов уже изменили. Показываю актуальные данные — повтори действие.",
        "refused": "Ядро не приняло изменение: синонимами бывают только опубликованные карточки одного типа. "
                   "Ничего не изменено.",
    },
    "uk": {
        "heading": "Синоніми ({card_type}):",
        "none": "Синонімів немає.",
        "this_card": "ця картка",
        "add_prompt": "Надішли слово або номер картки того ж типу.",
        "not_found": "Не знайшов картку того ж типу. Надішли інше слово або номер.",
        "all_in_group": "Знайшлася лише ця картка або картки, вже пов'язані з нею. Надішли інше слово або номер.",
        "refine": "Показано перші 10 знайдених, уточни запит.",
        "pick": "Обери картку, яку зв'язати:",
        "candidates": "Підібрано за спільним перекладом мовою словника. Зв'язати всі?",
        "candidates_more": "Показано перші 20.",
        "no_candidates": "Карток того ж типу з таким самим перекладом мовою словника немає.",
        "clear": "Очистити групу? Усі картки перестануть бути синонімами одна одної.",
        "stale": "Підтвердження застаріло. Повтори дію.",
        "conflict": "Групу синонімів уже змінили. Показую актуальні дані — повтори дію.",
        "refused": "Ядро не прийняло зміну: синонімами бувають лише опубліковані картки одного типу. "
                   "Нічого не змінено.",
    },
    "en": {
        "heading": "Synonyms ({card_type}):",
        "none": "No synonyms.",
        "this_card": "this card",
        "add_prompt": "Send a word or a number of a card of the same type.",
        "not_found": "No card of the same type found. Send another word or number.",
        "all_in_group": "Only this card or cards already linked to it were found. Send another word or number.",
        "refine": "Showing the first 10 found, refine the query.",
        "pick": "Choose the card to link:",
        "candidates": "Picked by a common translation in the dictionary language. Link them all?",
        "candidates_more": "Showing the first 20.",
        "no_candidates": "No cards of the same type share a translation in the dictionary language.",
        "clear": "Clear the group? None of its cards will be synonyms of each other any more.",
        "stale": "The confirmation is out of date. Repeat the action.",
        "conflict": "The synonym group has already been changed. Showing the current data — repeat the action.",
        "refused": "The core did not accept the change: only published cards of one type can be synonyms. "
                   "Nothing was changed.",
    },
}

# WORKING TEXT (not set by the specification): the synonym editor's button labels (project §3.5:
# "[✖ i]", "[➕ Добавить]", "[Подобрать по переводу]", "[Очистить группу]", "[Связать: …]",
# "[Подтвердить] [Отмена]"); final wording waits for the owner (T-TEXTS-PAGE).
SYNONYM_LABELS: dict[Language, dict[str, str]] = {
    "ru": {"remove": "✖ {index}", "add": "➕ Добавить", "candidates": "Подобрать по переводу",
           "clear": "Очистить группу", "link": "Связать: {lemma}", "confirm": "Подтвердить", "cancel": "Отмена",
           "back": "⬅ К карточке"},
    "uk": {"remove": "✖ {index}", "add": "➕ Додати", "candidates": "Підібрати за перекладом",
           "clear": "Очистити групу", "link": "Зв'язати: {lemma}", "confirm": "Підтвердити", "cancel": "Скасувати",
           "back": "⬅ До картки"},
    "en": {"remove": "✖ {index}", "add": "➕ Add", "candidates": "Pick by translation",
           "clear": "Clear the group", "link": "Link: {lemma}", "confirm": "Confirm", "cancel": "Cancel",
           "back": "⬅ To the card"},
}


def _data(*parts: object) -> str:
    return ":".join((CALLBACK_PREFIX, *(str(part) for part in parts)))


def version_code(version: int | None) -> str:
    return NO_GROUP if version is None else str(version)


@dataclass(frozen=True)
class SynonymPress:
    action: str
    card_id: UUID | None = None
    version: int | None = None
    index: int | None = None
    card_type: str | None = None
    key: str | None = None


def _version_of(value: str) -> tuple[bool, int | None]:
    if value == NO_GROUP:
        return True, None
    return (True, int(value)) if is_version(value) else (False, None)


def parse(data: str | None) -> SynonymPress | None:
    """The press of a synonym button, or None for anything malformed or foreign."""
    parts = (data or "").split(":")
    if len(parts) < 3 or parts[0] != CALLBACK_PREFIX:
        return None
    action, arguments = parts[1], parts[2:]
    if action in (PICK, CONFIRM):
        return SynonymPress(action, key=arguments[0]) if len(arguments) == 1 and _KEY.fullmatch(arguments[0]) else None
    card_id = card_of(arguments[0])
    if card_id is None:
        return None
    if action == VIEW:
        return SynonymPress(action, card_id) if len(arguments) == 1 else None
    if action in (CANDIDATES, CLEAR, ADD, REMOVE):
        size = {CANDIDATES: 2, CLEAR: 2, ADD: 2, REMOVE: 3}[action]
        if len(arguments) != size:
            return None
        if action == ADD:
            card_type = TYPE_OF_CODE.get(arguments[1])
            return None if card_type is None else SynonymPress(action, card_id, card_type=card_type)
        valid, version = _version_of(arguments[1])
        if not valid or (action in (CLEAR, REMOVE) and version is None):
            return None
        if action == REMOVE:
            if not _INDEX.fullmatch(arguments[2]):
                return None
            return SynonymPress(action, card_id, version, index=int(arguments[2]))
        return SynonymPress(action, card_id, version)
    return None


def _member_line(number: int, lemma: str, preview: str | None, own: bool, language: Language) -> str:
    line = f"{number}. {lemma}" + (f" — {preview}" if preview else "")
    return line + (f" ({SYNONYM_TEXTS[language]['this_card']})" if own else "")


def group_view(card_id: UUID, card_type: str, synonyms: SynonymList, language: Language) -> View:
    """The group of a card (§3.5): its members by card id with ``[✖ i]``, then add, pick, clear, back.

    The first INDEX_MAX members get a remove button (§4.6); the card itself is listed too:
    removing it takes it out of the group.
    """
    texts, labels = SYNONYM_TEXTS[language], SYNONYM_LABELS[language]
    code, version = card_code(card_id), synonyms.version
    lines = [texts["heading"].format(card_type=TYPE_LABELS[language][card_type])]
    lines += [_member_line(number, member.lemma, member.preview, member.card_id == card_id, language)
              for number, member in enumerate(synonyms.members, 1)] or [texts["none"]]
    removable = [CallbackButton(labels["remove"].format(index=number), _data(REMOVE, code, version, number))
                 for number in range(1, min(len(synonyms.members), INDEX_MAX) + 1)] if version is not None else []
    rows = [tuple(removable[start:start + 5]) for start in range(0, len(removable), 5)]
    rows.append((CallbackButton(labels["add"], _data(ADD, code, TYPE_CODES[card_type])),
                 CallbackButton(labels["candidates"], _data(CANDIDATES, code, version_code(version)))))
    if version is not None:
        rows.append((CallbackButton(labels["clear"], _data(CLEAR, code, version)),))
    rows.append((CallbackButton(labels["back"], f"ce:c:{code}"),))
    return View(fit("\n".join(lines)), tuple(rows))


def _choice_label(card: OwnerSearchCard) -> str:
    """The lemma, its first translation and level: two cards with one lemma are told apart."""
    return " · ".join([card.lemma + (f" — {card.preview}" if card.preview else "")] + ([card.cefr] if card.cefr else []))


def pick_view(choices: list[tuple[OwnerSearchCard, str]], more: bool, language: Language) -> View:
    """``[Связать: die Arbeit — работа · A2]`` per found card, each with its key (§3.5).

    Labels that would still be equal get a number; ``more`` (the search had other matches)
    asks to refine the query, as /find does.
    """
    labels, texts = SYNONYM_LABELS[language], SYNONYM_TEXTS[language]
    names = [_choice_label(card) for card, _ in choices]
    seen: dict[str, int] = {}
    rows = []
    for name, (_, key) in zip(names, choices):
        if names.count(name) > 1:
            seen[name] = seen.get(name, 0) + 1
            name = f"{name} ({seen[name]})"
        rows.append((CallbackButton(labels["link"].format(lemma=name), _data(PICK, key)),))
    text = texts["pick"] + (f"\n{texts['refine']}" if more else "")
    return View(text, tuple(rows))


def confirm_view(text: str, key: str, card_id: UUID, language: Language) -> View:
    labels = SYNONYM_LABELS[language]
    return View(fit(text), ((CallbackButton(labels["confirm"], _data(CONFIRM, key)),
                             CallbackButton(labels["cancel"], _data(VIEW, card_code(card_id)))),))


def candidates_text(cards: tuple[OwnerSearchCard, ...], more: bool, language: Language) -> str:
    texts = SYNONYM_TEXTS[language]
    lines = [texts["candidates"]]
    lines += [_member_line(number, card.lemma, card.preview, False, language) for number, card in enumerate(cards, 1)]
    if more:
        lines.append(texts["candidates_more"])
    return "\n".join(lines)
