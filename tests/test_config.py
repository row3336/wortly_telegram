"""Configuration of the bot and delivery roles: every key required, no value in the code."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import textwrap

import pytest

from wortly_telegram.config import BotSettings, DeliverySettings, TelegramSettings
from wortly_telegram.leader import LeaderConfig
from wortly_telegram.webapp import WebAppAddressError, checked_address


def telegram_settings(**overrides) -> TelegramSettings:
    values = dict(bot_token="1:x", web_app_url="https://app.example.test/", connect_timeout=timedelta(seconds=2),
                  read_timeout=timedelta(seconds=3), write_timeout=timedelta(seconds=2),
                  pool_timeout=timedelta(seconds=1))
    values.update(overrides)
    return TelegramSettings(**values)


@pytest.mark.parametrize("address", [
    "http://app.example.test/", "https://", "https:///path", "ftp://app.example.test/", "app.example.test",
    "https://user:pw@app.example.test/", "https://user@app.example.test/", " https://app.example.test/",
    "https://app.example.test/a b", "", None, 42, "https://[::1/",
])
def test_the_mini_app_address_must_be_plain_https(address):
    with pytest.raises(WebAppAddressError):
        checked_address(address)


def test_an_https_address_is_kept_as_written():
    assert checked_address("https://app.example.test/wortly/?v=2") == "https://app.example.test/wortly/?v=2"


def test_telegram_settings_hide_the_token_and_sum_the_request_budget():
    settings = telegram_settings()
    assert "1:x" not in repr(settings)
    assert settings.request_budget == timedelta(seconds=8)
    with pytest.raises(ValueError):
        telegram_settings(read_timeout=timedelta(0))
    with pytest.raises(WebAppAddressError):
        telegram_settings(web_app_url="http://app.example.test/")


def leader_config(**overrides) -> LeaderConfig:
    values = dict(holder="bot-1", lease=timedelta(seconds=60), stop_margin=timedelta(seconds=20),
                  standby_retry=timedelta(seconds=10))
    values.update(overrides)
    return LeaderConfig(**values)


def test_leader_renews_every_third_of_the_lease():
    assert leader_config().renew_period == timedelta(seconds=20)


@pytest.mark.parametrize("overrides", [
    {"holder": " bot"}, {"holder": ""}, {"lease": timedelta(0)}, {"stop_margin": timedelta(0)},
    {"standby_retry": timedelta(seconds=-1)},
    # The deadline must come after the first renewal at L/3.
    {"stop_margin": timedelta(seconds=40)},
])
def test_leader_config_refuses_unusable_values(overrides):
    with pytest.raises(ValueError):
        leader_config(**overrides)


def test_the_bot_stop_margin_must_cover_the_final_get_updates():
    BotSettings(database=None, telegram=telegram_settings(), leader=leader_config(),
                poll_timeout=timedelta(seconds=30), lock_timeout=timedelta(seconds=1),
                session_lifetime=timedelta(days=1))
    with pytest.raises(ValueError):
        BotSettings(database=None, telegram=telegram_settings(), leader=leader_config(stop_margin=timedelta(seconds=8)),
                    poll_timeout=timedelta(seconds=30), lock_timeout=timedelta(seconds=1),
                    session_lifetime=timedelta(days=1))


class _Loop:
    def __init__(self, lease: timedelta) -> None:
        self.lease_duration = lease


def test_the_job_lease_must_cover_a_slot_and_one_request():
    DeliverySettings(database=None, telegram=telegram_settings(), loop=_Loop(timedelta(seconds=9)),
                     lock_timeout=timedelta(seconds=1), messages_per_second=2)
    with pytest.raises(ValueError):
        # 8 s of timeouts + a 0.5 s slot leave nothing for the lease of 8 s.
        DeliverySettings(database=None, telegram=telegram_settings(), loop=_Loop(timedelta(seconds=8)),
                         lock_timeout=timedelta(seconds=1), messages_per_second=2)


# -- the loaders need the core's configuration reader, which needs psycopg ------------------

DATABASE = """
[database]
conninfo = "host=/tmp port=5432 dbname=wortly_test user=wortly sslmode=disable"
connect_timeout_seconds = 5
pool_min_size = 0
pool_max_size = 2
pool_timeout_seconds = 5
"""


def telegram_table(token_file: Path) -> str:
    return textwrap.dedent(f"""
        [telegram]
        bot_token_file = "{token_file}"
        web_app_url = "https://app.example.test/"
        connect_timeout_seconds = 2
        read_timeout_seconds = 3
        write_timeout_seconds = 2
        pool_timeout_seconds = 1
    """)


DELIVERY = """
[delivery]
instance = "delivery-1"
job_lease_seconds = 30
recovery_period_seconds = 60
poll_interval_seconds = 2
max_failed_preflights = 5
nondelivery_delay_seconds = 900
lock_timeout_milliseconds = 2000
messages_per_second = 20
"""

BOT = """
[bot]
instance = "bot-1"
leader_lease_seconds = 60
stop_margin_seconds = 15
standby_retry_seconds = 10
poll_timeout_seconds = 25
lock_timeout_milliseconds = 2000

[access]
session_lifetime_seconds = 86400
"""


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("123:secret\n")
    return path


def test_delivery_settings_load_every_key(tmp_path, token_file):
    pytest.importorskip("psycopg")
    from wortly_telegram.config import load_delivery_settings
    path = tmp_path / "delivery.toml"
    path.write_text(DATABASE + DELIVERY + telegram_table(token_file))
    settings = load_delivery_settings(path)
    assert settings.loop.owner == "delivery-1"
    assert settings.loop.lease_duration == timedelta(seconds=30)
    assert settings.loop.retry.nondelivery_delay == timedelta(seconds=900)
    assert settings.lock_timeout == timedelta(milliseconds=2000)
    assert settings.telegram.bot_token == "123:secret"
    assert "123:secret" not in repr(settings)


@pytest.mark.parametrize("drop", ["messages_per_second", "job_lease_seconds", "web_app_url", "instance"])
def test_delivery_settings_without_a_key_stop_the_start(tmp_path, token_file, drop):
    pytest.importorskip("psycopg")
    from wortly.operations.config import ConfigurationError
    from wortly_telegram.config import load_delivery_settings
    text = DATABASE + DELIVERY + telegram_table(token_file)
    text = "\n".join(line for line in text.splitlines() if not line.startswith(drop))
    path = tmp_path / "delivery.toml"
    path.write_text(text)
    with pytest.raises(ConfigurationError) as raised:
        load_delivery_settings(path)
    assert drop in str(raised.value)


def test_bot_settings_load_every_key_and_refuse_unknown_ones(tmp_path, token_file):
    pytest.importorskip("psycopg")
    from wortly.operations.config import ConfigurationError
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(DATABASE + BOT + telegram_table(token_file))
    settings = load_bot_settings(path)
    assert settings.leader.renew_period == timedelta(seconds=20)
    assert settings.poll_timeout == timedelta(seconds=25)
    path.write_text(DATABASE + BOT + telegram_table(token_file) + "allowed_updates = 1\n")
    with pytest.raises(ConfigurationError):
        load_bot_settings(path)
