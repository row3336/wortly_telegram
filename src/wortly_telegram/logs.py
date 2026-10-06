"""Technical logs of the bot and delivery processes (TC:273; design T-D §2.2; O-OBS).

Both process entries run inside the core's record factory
(wortly.operations.log_privacy.private_records): an exception is written by type names
only, and the secret values given here are replaced wherever a library writes them.

- The bot token in every written form (the core's token_forms): PTB puts it into each
  request URL, uvicorn-like writers percent-encode its colon.
- Delivery: both connection strings of the database settings (as configured and as
  libpq gets it), through log_secrets.
- Bot: the service secret of the core's internal API; the bot opens no database
  (answer 2.4), so bot_main passes the token forms and the secret itself.

Library loggers are kept at WARNING: at DEBUG and INFO, PTB and httpx write request
URLs, message texts, Telegram names and answers. The adapter's own code logs outcome
classes and exception classes only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wortly.operations.config import DatabaseSettings


LIBRARY_LOGGERS = ("httpx", "httpcore", "telegram", "hpack", "psycopg")


def quiet_libraries() -> None:
    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def log_secrets(token: str, database: DatabaseSettings) -> tuple[str, ...]:
    from wortly.operations.connections import writer_conninfo
    from wortly.operations.log_privacy import token_forms

    return (*token_forms(token), database.conninfo, writer_conninfo(database))


@contextmanager
def private_logs(secrets: tuple[str, ...]) -> Iterator[None]:
    """Quiet the libraries and clean every record created inside the block."""
    from wortly.operations.log_privacy import private_records

    quiet_libraries()
    with private_records(secrets):
        yield
