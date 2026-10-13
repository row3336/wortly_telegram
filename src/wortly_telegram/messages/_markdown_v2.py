"""MarkdownV2 escapes of the two narrow exceptions to ВП-16 (R9-04, R9-05; К19-049; TZC:470).

Every other message of the bot is plain text without parse_mode. Only the ``#daily`` report
(a monospace block) and the copyable echo of a common field in the service bot are sent with
``parse_mode="MarkdownV2"``, and each value they place is escaped here first, so no fact and
no user's text can end the block or add markup (Bot API, "MarkdownV2 style").

A helper module: its name starts with "_", so the template registry skips it.
"""

from __future__ import annotations


# Outside an entity Telegram needs each of these escaped; "\\" itself too (Bot API MarkdownV2 style).
_SPECIAL = frozenset("\\_*[]()~`>#+-=|{}.!")


def escape_pre(text: str) -> str:
    """Escape ``text`` inside a ``pre`` or ``code`` entity: only "\\" and "`" (Bot API, MarkdownV2 style)."""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def escape_text(text: str) -> str:
    """Escape ``text`` outside any entity: every character MarkdownV2 reserves gets a backslash."""
    return "".join(f"\\{char}" if char in _SPECIAL else char for char in text)
