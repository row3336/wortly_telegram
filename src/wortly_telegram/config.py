"""Explicit configuration of the bot and delivery roles (design T-D §2.2).

One TOML file per process, named on the command line. Every key is required, the code
holds no value for any of them, and an unknown key stops the start. The file format,
the [database] table and the error style (the key is named, never a value) are the
core's (wortly.operations.config), so every role is configured the same way. The bot
token never lives in the file: telegram.bot_token_file names the file that holds it.

Loading needs the database driver (the core checks the connection string with it).
The core is therefore imported inside the loaders; handlers that import this module
for its types run without psycopg.

Relations checked here are technical, not policy:

- delivery: a job lease must outlast one send slot plus one Telegram request with all
  its timeouts, because the lease is taken before the slot wait (design §2.2, §3.5 п.4);
- bot: the leader must stop polling before its lease can expire, and stopping ends with
  one more getUpdates, so the stop margin must cover one request with all its timeouts
  (§2.2; PTB Updater._stop_polling).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .leader import LeaderConfig
from .webapp import WebAppAddressError, checked_address

if TYPE_CHECKING:
    from wortly.jobs.loop import DeliveryLoopConfig
    from wortly.operations.config import DatabaseSettings


# PostgreSQL keeps lock_timeout as an integer number of milliseconds (as the worker).
_LONGEST_LOCK_WAIT_MS = 2**31 - 1
# Seconds a TOML integer may name without overflowing a timedelta.
_LONGEST_SECONDS = 999_999_999 * 86_400


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


@dataclass(frozen=True)
class BotSettings:
    database: DatabaseSettings
    telegram: TelegramSettings
    leader: LeaderConfig
    poll_timeout: timedelta
    lock_timeout: timedelta
    # The core IdentityService requires it; the bot never opens an access session
    # (it never calls login), so the value has no effect here. Question to root.
    session_lifetime: timedelta

    def __post_init__(self) -> None:
        if self.leader.stop_margin <= self.telegram.request_budget:
            raise ValueError("bot stop_margin_seconds must exceed the sum of the telegram request timeouts")


def _telegram(table) -> TelegramSettings:
    url = table.text("web_app_url")
    try:
        checked_address(url)
    except WebAppAddressError as error:
        from wortly.operations.config import ConfigurationError
        raise ConfigurationError(f"telegram.web_app_url: {error}") from None
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


def _checked(build):
    """Run ``build``; a value error of a settings object becomes a configuration error."""
    from wortly.operations.config import ConfigurationError
    try:
        return build()
    except ConfigurationError:
        raise
    except ValueError as error:
        # Settings objects name the faulty setting, never its value.
        raise ConfigurationError(str(error)) from None


def load_delivery_settings(path: Path) -> DeliverySettings:
    """Read the delivery configuration; raise ConfigurationError if anything is missing.

    Tables: [database] (as the api role), [delivery], [telegram].
    """
    from wortly.jobs.loop import DeliveryLoopConfig
    from wortly.jobs.retry_policy import RetryPolicyConfig
    from wortly.operations.config import load_database_settings, read_config_file

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

    settings = _checked(build)
    for table in (root, database, delivery, telegram):
        table.finish()
    return settings


def load_bot_settings(path: Path) -> BotSettings:
    """Read the bot configuration; raise ConfigurationError if anything is missing.

    Tables: [database] (as the api role), [bot], [access], [telegram].
    """
    from wortly.operations.config import load_database_settings, read_config_file

    root = read_config_file(path)
    database, bot, access, telegram = (root.table("database"), root.table("bot"), root.table("access"),
                                       root.table("telegram"))

    def build() -> BotSettings:
        leader = LeaderConfig(
            holder=bot.text("instance"),
            lease=_seconds(bot, "leader_lease_seconds"),
            stop_margin=_seconds(bot, "stop_margin_seconds"),
            standby_retry=_seconds(bot, "standby_retry_seconds"))
        return BotSettings(
            database=load_database_settings(database), telegram=_telegram(telegram), leader=leader,
            poll_timeout=_seconds(bot, "poll_timeout_seconds"), lock_timeout=_lock_timeout(bot),
            session_lifetime=access.lifetime_from_now("session_lifetime_seconds"))

    settings = _checked(build)
    for table in (root, database, bot, access, telegram):
        table.finish()
    return settings
