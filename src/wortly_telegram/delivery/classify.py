"""What a failed Bot API call proves about the message (design T-D §3.3).

PTB 22.8 turns answers into exceptions (request/_baserequest.py, _httpxrequest.py).
BadRequest and TimedOut are subclasses of NetworkError, so the order of the checks is
the rule itself: first the answers Telegram gave explicitly (RetryAfter, Forbidden,
ChatMigrated, BadRequest, InvalidToken), then a request that provably never left
(the httpx cause of the failure), and only then the general TimedOut / NetworkError,
which may follow an actual delivery and are therefore unknown.

- 429 without retry_after reaches PTB's generic branch as a NetworkError with no
  cause; without parsing text it cannot be told from a 5xx, so it is unknown.
- "Never left" (ВП-10, precedent OLD bot/common/navigation.py:108-117): the cause is
  httpx.ConnectError, ConnectTimeout or PoolTimeout.
- Anything else (read/write timeouts, broken connections, 5xx, Conflict, a body PTB
  could not parse after a 2xx, any other exception) is unknown and never repeated.

- 403 (answer 2.5): only Telegram's own words "bot was blocked by the user" confirm that
  the recipient blocked the bot. Every other 403 (the user never started the dialog, the
  account is deleted, a text this code does not know) proves nothing about a block, so it
  is "forbidden" and stays repeatable: the safe side. The text is matched here and only
  here; PTB keeps "Forbidden: ..." in the message. The exact wording on live Telegram is
  not checked locally: to be verified at A-PHONE.

Only the class of the proof is returned; the text of Telegram's answer never is (TC:273).
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from telegram.error import BadRequest, ChatMigrated, Forbidden, InvalidToken, NetworkError, RetryAfter

from wortly.jobs.channel import UNKNOWN, ChannelResult


NOT_SENT_CAUSES = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

BLOCKED_MARKER = "bot was blocked by the user"

BLOCKED = ChannelResult("known_failed", "blocked")
FORBIDDEN = ChannelResult("known_failed", "forbidden")
REJECTED = ChannelResult("known_failed", "rejected")
CHANNEL_CONFIG = ChannelResult("known_failed", "channel_config")
NOT_SENT = ChannelResult("known_failed", "not_sent")
PRE_SEND_ERROR = ChannelResult("known_failed", "pre_send_error")


def retry_after(error: RetryAfter) -> timedelta:
    value = error.retry_after
    return value if isinstance(value, timedelta) else timedelta(seconds=value)


def never_left(error: BaseException) -> bool:
    """True when the request certainly did not reach Telegram (the httpx cause says so)."""
    return isinstance(error, NetworkError) and isinstance(error.__cause__, NOT_SENT_CAUSES)


def classify(error: BaseException) -> ChannelResult:
    """Map the exception of one send_message call to the channel's result."""
    if isinstance(error, RetryAfter):
        return ChannelResult("known_failed", "rate_limited", retry_after(error))
    if isinstance(error, Forbidden):
        return BLOCKED if BLOCKED_MARKER in error.message.casefold() else FORBIDDEN
    if isinstance(error, (ChatMigrated, BadRequest)):
        return REJECTED
    if isinstance(error, InvalidToken):
        return CHANNEL_CONFIG
    if never_left(error):
        return NOT_SENT
    return UNKNOWN
