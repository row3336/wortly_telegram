"""Explicit configuration of the bot and delivery roles (design T-D §2.2).

One TOML file per process, named on the command line. Every key is required, the code
holds no value for any of them, and an unknown key stops the start. The file format,
the [database] table and the error style (the key is named, never a value) are the
core's (wortly.operations.config), so every role is configured the same way. Secrets
never live in the file: telegram.bot_token_file and core.service_secret_file name the
files that hold them, and a file open to group or others stops the start (C-CONFIG-23).

The delivery role opens the database, and its loader uses the core's reader, which
needs the database driver; the core is imported inside that loader. The bot opens no
database (answer 2.4) and runs without psycopg, so it has no [database] and no
[access] table (answer 2.7а) and is read by ``_BotTable`` below: the same rules as the
core's ConfigTable, without the driver the core's module imports at its top. The core
reader is not importable without psycopg; once it is, ``_BotTable`` goes.

Relations checked here are technical, not policy:

- delivery: a job lease must outlast one send slot plus one Telegram request with all
  its timeouts, because the lease is taken before the slot wait (design §2.2, §3.5 п.4);
- bot: the leader must stop polling before its lease can expire, and stopping ends with
  one more getUpdates, so the stop margin must cover one request with all its timeouts
  (§2.2; PTB Updater._stop_polling); one call to the core must end within the margin
  too, so a renewal that fails by its own timeout still leaves time to stop; and that
  call must end before L - margin, or a slow acquire is always given back unused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import os
from pathlib import Path
import stat
import tomllib
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .leader import LeaderConfig
from .webapp import WebAppAddressError, checked_address

if TYPE_CHECKING:
    from wortly.jobs.loop import DeliveryLoopConfig
    from wortly.operations.config import DatabaseSettings


# PostgreSQL keeps lock_timeout as an integer number of milliseconds (as the worker).
_LONGEST_LOCK_WAIT_MS = 2**31 - 1
# Seconds a TOML integer may name without overflowing a timedelta.
_LONGEST_SECONDS = 999_999_999 * 86_400
# Any group or other permission bit on a secret file: read, write or execute (as the core).
_OTHERS_ACCESS = stat.S_IRWXG | stat.S_IRWXO


class BotConfigurationError(ValueError):
    """The bot configuration is incomplete or invalid; the message names the key, never a value."""


@dataclass(frozen=True)
class TelegramSettings:
    """The Bot API client of one role: token, Mini App address, request timeouts."""

    bot_token: str = field(repr=False)
    web_app_url: str
    connect_timeout: timedelta
    read_timeout: timedelta
    write_timeout: timedelta
    pool_timeout: timedelta

    def __post_init__(self) -> None:
        for name in ("connect_timeout", "read_timeout", "write_timeout", "pool_timeout"):
            value = getattr(self, name)
            if type(value) is not timedelta or value <= timedelta(0):
                raise ValueError(f"telegram {name} must be a positive duration")
        checked_address(self.web_app_url)

    @property
    def request_budget(self) -> timedelta:
        """The longest one Bot API request can take: waiting for a connection, connecting,
        writing and reading (PTB HTTPXRequest passes all four to httpx)."""
        return self.pool_timeout + self.connect_timeout + self.write_timeout + self.read_timeout


@dataclass(frozen=True)
class DeliverySettings:
    database: DatabaseSettings
    telegram: TelegramSettings
    loop: DeliveryLoopConfig
    lock_timeout: timedelta
    messages_per_second: int

    def __post_init__(self) -> None:
        slot = timedelta(seconds=1) / self.messages_per_second
        if self.loop.lease_duration <= slot + self.telegram.request_budget:
            raise ValueError("delivery job_lease_seconds must exceed one send slot and "
                             "the sum of the telegram request timeouts")


def checked_internal_url(value: object) -> str:
    """Return ``value`` if it names only a scheme, a host and a port: the routes add the path."""
    if type(value) is not str or not value or any(character.isspace() for character in value):
        raise ValueError("core internal_url must be a URL without blanks")
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        credentials = parts.username is not None or parts.password is not None
    except ValueError:
        raise ValueError("core internal_url is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host or port == 0:
        raise ValueError("core internal_url must be an http or https URL with a host")
    if credentials or parts.path or parts.query or parts.fragment or value.endswith(("?", "#")):
        # A path would shift every route; a trailing "/" would make the core answer 404.
        raise ValueError("core internal_url must hold no credentials, path, query or fragment")
    return value


@dataclass(frozen=True)
class CoreSettings:
    """The core's internal API as the bot calls it (answer 2.4): address, secret, timeouts."""

    internal_url: str
    service_secret: str = field(repr=False)
    connect_timeout: timedelta
    read_timeout: timedelta

    def __post_init__(self) -> None:
        checked_internal_url(self.internal_url)
        for name in ("connect_timeout", "read_timeout"):
            value = getattr(self, name)
            if type(value) is not timedelta or value <= timedelta(0):
                raise ValueError(f"core {name} must be a positive duration")
        secret = self.service_secret
        # An HTTP header carries printable ASCII only; any other secret would never match
        # the core's file and every call would silently get 401 (core README, step 2а).
        if (type(secret) is not str or not secret or secret != secret.strip()
                or not all(" " <= character <= "~" for character in secret)):
            raise ValueError("core.service_secret_file must hold printable ASCII without surrounding blanks")

    @property
    def request_budget(self) -> timedelta:
        """The deadline of one call to the core (gateway.InternalClient)."""
        return self.connect_timeout + self.read_timeout


@dataclass(frozen=True)
class BotSettings:
    telegram: TelegramSettings
    core: CoreSettings
    leader: LeaderConfig
    poll_timeout: timedelta

    def __post_init__(self) -> None:
        if self.leader.stop_margin <= self.telegram.request_budget:
            raise ValueError("bot stop_margin_seconds must exceed the sum of the telegram request timeouts")
        if self.leader.stop_margin <= self.core.request_budget:
            raise ValueError("bot stop_margin_seconds must exceed the sum of the core request timeouts")
        if self.core.request_budget >= self.leader.lease - self.leader.stop_margin:
            # An acquire may answer as late as the core deadline; answered after
            # L - margin, the leader gives the lease back (leader.py), so every slow
            # acquire would be given back and the bot would never poll.
            raise ValueError("core connect_timeout_seconds + read_timeout_seconds must be shorter than "
                             "bot leader_lease_seconds - stop_margin_seconds")


def _telegram(table) -> TelegramSettings:
    url = table.text("web_app_url")
    try:
        checked_address(url)
    except WebAppAddressError as error:
        # _checked turns it into the loader's configuration error.
        raise ValueError(f"telegram.web_app_url: {error}") from None
    return TelegramSettings(
        bot_token=table.secret_file("bot_token_file"),
        web_app_url=url,
        connect_timeout=_seconds(table, "connect_timeout_seconds"),
        read_timeout=_seconds(table, "read_timeout_seconds"),
        write_timeout=_seconds(table, "write_timeout_seconds"),
        pool_timeout=_seconds(table, "pool_timeout_seconds"))


def _seconds(table, key: str) -> timedelta:
    return timedelta(seconds=table.integer(key, minimum=1, maximum=_LONGEST_SECONDS))


def _lock_timeout(table) -> timedelta:
    return timedelta(milliseconds=table.integer("lock_timeout_milliseconds", minimum=1,
                                                maximum=_LONGEST_LOCK_WAIT_MS))


def _checked(build, error: type[ValueError]):
    """Run ``build``; a value error of a settings object becomes the loader's ``error``."""
    try:
        return build()
    except error:
        raise
    except ValueError as failure:
        # Settings objects name the faulty setting, never its value.
        raise error(str(failure)) from None


# Why a copy: the core's operations.config imports psycopg at module level, and the bot
# runs without it. Duplicated until the core extracts a psycopg-free config module
# (planned core package); then this class and _read_bot_file go.
class _BotTable:
    """One TOML table of the bot file, read as the core's ConfigTable reads it."""

    def __init__(self, values: dict, name: str) -> None:
        self._values, self._name, self._read = values, name, set()

    def _key(self, key: str) -> str:
        return f"{self._name}.{key}" if self._name else key

    def _value(self, key: str):
        self._read.add(key)
        if key not in self._values:
            raise BotConfigurationError(f"Missing required key {self._key(key)}")
        return self._values[key]

    def table(self, key: str) -> _BotTable:
        value = self._value(key)
        if not isinstance(value, dict):
            raise BotConfigurationError(f"{self._key(key)} must be a table")
        return _BotTable(value, self._key(key))

    def text(self, key: str) -> str:
        value = self._value(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise BotConfigurationError(f"{self._key(key)} must be a non-empty string without surrounding blanks")
        return value

    def integer(self, key: str, *, minimum: int, maximum: int | None = None) -> int:
        value = self._value(key)
        # TOML booleans are Python ints; a flag is never a valid count or duration.
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            limits = f"{minimum}..{maximum}" if maximum is not None else f">= {minimum}"
            raise BotConfigurationError(f"{self._key(key)} must be an integer {limits}")
        return value

    def secret_file(self, key: str) -> str:
        path = Path(self.text(key))
        if not path.is_absolute():
            raise BotConfigurationError(f"{self._key(key)} must be an absolute path")
        try:
            # One descriptor for the check and the read, as the core: the checked file is the
            # read file. O_NONBLOCK keeps a named pipe from stalling the open.
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            raise BotConfigurationError(f"{self._key(key)} cannot be read") from None
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise BotConfigurationError(f"{self._key(key)} is not a regular file")
            if status.st_mode & _OTHERS_ACCESS or status.st_uid != os.geteuid():
                raise BotConfigurationError(f"{self._key(key)} must be owned by the process user and "
                                            "closed to group and others (mode 0600)")
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                secret = file.read().decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise BotConfigurationError(f"{self._key(key)} cannot be read") from None
        finally:
            os.close(descriptor)
        if not secret:
            raise BotConfigurationError(f"{self._key(key)} names an empty file")
        if len(secret.splitlines()) != 1:
            raise BotConfigurationError(f"{self._key(key)} must hold the secret on a single line")
        return secret

    def finish(self) -> None:
        unknown = sorted(set(self._values) - self._read)
        if unknown:
            raise BotConfigurationError("Unknown configuration key " + ", ".join(self._key(key) for key in unknown))


def _read_bot_file(path: Path) -> _BotTable:
    try:
        with path.open("rb") as file:
            return _BotTable(tomllib.load(file), "")
    except OSError:
        raise BotConfigurationError("Configuration file cannot be read") from None
    except UnicodeDecodeError:
        raise BotConfigurationError("Configuration file is not UTF-8 text") from None
    except tomllib.TOMLDecodeError as error:
        raise BotConfigurationError(f"Configuration file is not valid TOML (line {error.lineno})") from None


def load_delivery_settings(path: Path) -> DeliverySettings:
    """Read the delivery configuration; raise ConfigurationError if anything is missing.

    Tables: [database] (as the api role), [delivery], [telegram].
    """
    from wortly.jobs.loop import DeliveryLoopConfig
    from wortly.jobs.retry_policy import RetryPolicyConfig
    from wortly.operations.config import ConfigurationError, load_database_settings, read_config_file

    root = read_config_file(path)
    database, delivery, telegram = root.table("database"), root.table("delivery"), root.table("telegram")

    def build() -> DeliverySettings:
        loop = DeliveryLoopConfig(
            owner=delivery.text("instance"),
            lease_duration=_seconds(delivery, "job_lease_seconds"),
            recovery_period=_seconds(delivery, "recovery_period_seconds"),
            poll_interval=_seconds(delivery, "poll_interval_seconds"),
            max_failed_preflights=delivery.integer("max_failed_preflights", minimum=1, maximum=2**31 - 1),
            retry=RetryPolicyConfig(nondelivery_delay=_seconds(delivery, "nondelivery_delay_seconds")))
        return DeliverySettings(
            database=load_database_settings(database), telegram=_telegram(telegram), loop=loop,
            lock_timeout=_lock_timeout(delivery),
            messages_per_second=delivery.integer("messages_per_second", minimum=1, maximum=1_000_000))

    settings = _checked(build, ConfigurationError)
    for table in (root, database, delivery, telegram):
        table.finish()
    return settings


def load_bot_settings(path: Path) -> BotSettings:
    """Read the bot configuration; raise BotConfigurationError if anything is missing.

    Tables: [bot], [core], [telegram]. Neither the core nor psycopg is imported.
    """
    root = _read_bot_file(path)
    bot, core, telegram = root.table("bot"), root.table("core"), root.table("telegram")

    def build() -> BotSettings:
        leader = LeaderConfig(
            holder=bot.text("instance"),
            lease=_seconds(bot, "leader_lease_seconds"),
            stop_margin=_seconds(bot, "stop_margin_seconds"),
            standby_retry=_seconds(bot, "standby_retry_seconds"))
        internal = CoreSettings(
            internal_url=core.text("internal_url"),
            service_secret=core.secret_file("service_secret_file"),
            connect_timeout=_seconds(core, "connect_timeout_seconds"),
            read_timeout=_seconds(core, "read_timeout_seconds"))
        return BotSettings(telegram=_telegram(telegram), core=internal, leader=leader,
                           poll_timeout=_seconds(bot, "poll_timeout_seconds"))

    settings = _checked(build, BotConfigurationError)
    for table in (root, bot, core, telegram):
        table.finish()
    return settings
