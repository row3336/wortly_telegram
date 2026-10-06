"""Technical logs of the bot and delivery processes (TC:273; design T-D §2.2; O-OBS).

Both process entries run inside the core's record factory
(wortly.operations.log_privacy.private_records): an exception is written by type names
only, and the secret values given here are replaced wherever a library writes them.

- The bot token in every written form. PTB puts it into each request URL, uvicorn-like
  writers percent-encode its colon; the part after the colon is never encoded, so
  hiding it hides every form.
- Both connection strings of the database settings (as configured and as libpq gets it).

Library loggers are kept at WARNING: at DEBUG and INFO, PTB and httpx write request
URLs, message texts, Telegram names and answers. The adapter's own code logs outcome
classes and exception classes only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from wortly.operations.config import DatabaseSettings


LIBRARY_LOGGERS = ("httpx", "httpcore", "telegram", "hpack", "psycopg")


def quiet_libraries() -> None:
    for name in LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def token_forms(token: str) -> tuple[str, ...]:
    """The token as written, percent-encoded, and its secret part after the colon."""
    return token, quote(token, safe=""), token.partition(":")[2]


def log_secrets(token: str, database: DatabaseSettings) -> tuple[str, ...]:
    from wortly.operations.connections import writer_conninfo

    return (*token_forms(token), database.conninfo, writer_conninfo(database))


@contextmanager
def private_logs(secrets: tuple[str, ...]) -> Iterator[None]:
    """Quiet the libraries and clean every record created inside the block."""
    from wortly.operations.log_privacy import private_records

    quiet_libraries()
    with private_records(secrets):
        yield
