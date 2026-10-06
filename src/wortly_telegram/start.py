"""/start: a short text and the button that opens the Mini App (design T-D §6; К00-016, К00-017, К00-019).

Everyone gets the same answer: admission happens in the Mini App (login with initData,
design §5.1 Б1), not in the chat. The reply is in the saved interface language of the
sender's profile, read through the core gateway, else in the Telegram client's language
by the core's rule (§4.2). If the core cannot answer, the reply still goes out in the
client's language: it carries nothing from the profile and the button works without it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update

from .gateway import principal_of
from .messages import Language, reply_language
from .reply import send_reply
from .webapp import launch_keyboard

if TYPE_CHECKING:
    from .router import BotContext


logger = logging.getLogger(__name__)

# Text not set by the specification (returned to root, Q20/Q23): the old bot's reply to
# /start for an admitted user (OLD bot/i18n.py "main_menu"; R-TG 1.01), above the button.
TEXTS: dict[Language, str] = {
    "ru": "Главное меню:",
    "uk": "Головне меню:",
    "en": "Main menu:",
}


async def language_of(update: Update, context: BotContext) -> Language:
    try:
        summary = await context.gateway.profile_summary(principal_of(update))
    except Exception as error:  # noqa: BLE001 - the reply needs no profile
        logger.warning("profile summary unavailable: %s", type(error).__name__)
        summary = None
    saved = summary.get("ui_language") if summary else None
    return reply_language(saved, update.effective_user.language_code)


async def send_start(update: Update, context: BotContext) -> None:
    """Answer with the text and the Mini App button."""
    language = await language_of(update, context)
    chat_id = update.effective_chat.id
    keyboard = launch_keyboard(context.web_app_url, language)
    await send_reply(lambda: context.bot.send_message(chat_id=chat_id, text=TEXTS[language],
                                                      reply_markup=keyboard),
                     sleep=context.sleep)
