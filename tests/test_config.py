"""Configuration of the bot and delivery roles: every key required, no value in the code.

The bot's loader needs neither psycopg nor the core's reader (answer 2.4), so its tests
run in both interpreters; the delivery loader's tests need psycopg.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest

from fakes.core_gateway import CORE_SECRET, core_settings
from test_import_without_psycopg import probe_environment
from wortly_telegram.config import (
    BotConfigurationError, BotSettings, CoreSettings, DeliverySettings, TelegramSettings, checked_internal_url,
)
from wortly_telegram.leader import LeaderConfig
from wortly_telegram.webapp import WebAppAddressError, checked_address


# 32 bytes as 64 lowercase hex characters: the only form the loader accepts.
SERVICE_SECRET = "0123456789abcdef" * 4
KEY_FORMAT_ERROR = ("core.service_secret_file must hold exactly 64 lowercase hexadecimal "
                    "characters (0-9, a-f)")


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


def bot_settings(**overrides) -> BotSettings:
    values = dict(telegram=telegram_settings(), core=core_settings(), leader=leader_config(),
                  poll_timeout=timedelta(seconds=30))
    values.update(overrides)
    return BotSettings(**values)


def test_the_bot_stop_margin_must_cover_the_final_get_updates():
    bot_settings()
    with pytest.raises(ValueError, match="telegram"):
        bot_settings(leader=leader_config(stop_margin=timedelta(seconds=8)))


@pytest.mark.parametrize(("read", "accepted"), [(18, True), (19, False), (25, False)])
def test_the_bot_stop_margin_must_outlast_one_call_to_the_core(read, accepted):
    # Margin 20 s, connect 1 s: a call may take connect + read in all.
    core = core_settings(connect_timeout=timedelta(seconds=1), read_timeout=timedelta(seconds=read))
    if accepted:
        bot_settings(core=core)
    else:
        with pytest.raises(ValueError, match="core request timeouts"):
            bot_settings(core=core)


@pytest.mark.parametrize(("read", "accepted"), [(23, True), (24, False)])
def test_one_call_to_the_core_must_end_before_the_lease_minus_the_margin(read, accepted):
    # L 70 s, margin 45 s: an acquire answered after 25 s is given back unused, so the
    # core deadline (connect 1 s + read) must stay below 25 s, though it is below the margin.
    leader = leader_config(lease=timedelta(seconds=70), stop_margin=timedelta(seconds=45))
    core = core_settings(connect_timeout=timedelta(seconds=1), read_timeout=timedelta(seconds=read))
    if accepted:
        bot_settings(leader=leader, core=core)
    else:
        with pytest.raises(ValueError, match="leader_lease_seconds - stop_margin_seconds"):
            bot_settings(leader=leader, core=core)


def test_core_settings_hide_the_secret_and_sum_the_call_deadline():
    settings = core_settings()
    assert CORE_SECRET not in repr(settings)
    assert settings.request_budget == timedelta(seconds=3)
    with pytest.raises(ValueError):
        core_settings(read_timeout=timedelta(0))


@pytest.mark.parametrize("secret", ["", " padded", "padded ", "ключ", "tab\tinside", "line\nbreak", "\x7f", 42])
def test_the_service_secret_is_printable_ascii_without_surrounding_blanks(secret):
    with pytest.raises(ValueError):
        core_settings(service_secret=secret)


@pytest.mark.parametrize("address", ["http://127.0.0.1:8001", "https://core.internal.test:8443", "http://[::1]:8001",
                                     "http://core.internal.test"])
def test_an_internal_address_of_scheme_host_and_port_is_kept(address):
    assert checked_internal_url(address) == address


@pytest.mark.parametrize("address", [
    "http://127.0.0.1:8001/", "http://127.0.0.1:8001/internal", "http://127.0.0.1:8001?x=1", "http://127.0.0.1:8001#a",
    "http://127.0.0.1:8001?", "ftp://127.0.0.1:8001", "127.0.0.1:8001", "http://", "http://:8001",
    "http://user:pw@127.0.0.1:8001", "http://127.0.0.1:0", "http://127.0.0.1:99999", " http://127.0.0.1:8001",
    "http://127.0.0.1:8001 ", "", None, 8001,
])
def test_an_internal_address_with_more_or_less_is_refused(address):
    with pytest.raises(ValueError):
        checked_internal_url(address)


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


@pytest.mark.parametrize(("seconds", "accepted"), [(8.4, False), (8.5, False), (8.6, True)])
def test_the_job_lease_must_cover_the_slot_beyond_the_request_timeouts(seconds, accepted):
    # 8 s of timeouts and a 0.5 s slot: 8.4 s outlasts the timeouts alone but not the slot too.
    def build() -> DeliverySettings:
        return DeliverySettings(database=None, telegram=telegram_settings(), loop=_Loop(timedelta(seconds=seconds)),
                                lock_timeout=timedelta(seconds=1), messages_per_second=2)

    if accepted:
        build()
    else:
        with pytest.raises(ValueError):
            build()


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
"""


def core_table(secret_file: Path) -> str:
    return textwrap.dedent(f"""
        [core]
        internal_url = "http://127.0.0.1:8001"
        service_secret_file = "{secret_file}"
        connect_timeout_seconds = 2
        read_timeout_seconds = 5
    """)


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text("123:secret\n")
    # The core refuses a secret file open to group or others, whatever the umask is.
    path.chmod(0o600)
    return path


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "service_secret"
    path.write_text(SERVICE_SECRET + "\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def bot_text(token_file, secret_file) -> str:
    return BOT + core_table(secret_file) + telegram_table(token_file)


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


def test_bot_settings_load_every_key_and_refuse_unknown_ones(tmp_path, bot_text):
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    settings = load_bot_settings(path)
    assert settings.leader.renew_period == timedelta(seconds=20)
    assert settings.poll_timeout == timedelta(seconds=25)
    assert settings.core == CoreSettings(internal_url="http://127.0.0.1:8001", service_secret=SERVICE_SECRET,
                                         connect_timeout=timedelta(seconds=2), read_timeout=timedelta(seconds=5))
    assert settings.telegram.bot_token == "123:secret"
    assert SERVICE_SECRET not in repr(settings) and "123:secret" not in repr(settings)
    path.write_text(bot_text + "allowed_updates = 1\n")
    with pytest.raises(BotConfigurationError):
        load_bot_settings(path)


@pytest.mark.parametrize("table", [DATABASE, "\n[access]\nsession_lifetime_seconds = 86400\n"],
                         ids=["database", "access"])
def test_the_bot_has_no_database_and_no_access_table(tmp_path, bot_text, table):
    # Answers 2.4 and 2.7а: a table of the old variant A stops the start instead of being ignored.
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(table + bot_text)
    with pytest.raises(BotConfigurationError, match="Unknown configuration key (database|access)"):
        load_bot_settings(path)
    path.write_text(bot_text.replace("[bot]\n", "[bot]\nlock_timeout_milliseconds = 2000\n", 1))
    with pytest.raises(BotConfigurationError, match="bot.lock_timeout_milliseconds"):
        load_bot_settings(path)


@pytest.mark.parametrize("drop", ["internal_url", "service_secret_file", "connect_timeout_seconds",
                                  "read_timeout_seconds", "instance", "bot_token_file", "[core]"])
def test_bot_settings_without_a_key_stop_the_start(tmp_path, bot_text, drop):
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    if drop == "[core]":
        text = bot_text.replace("[core]\n", "[core_old]\n")
    else:
        # The first occurrence: [core] comes before [telegram], which repeats the timeout names.
        lines = bot_text.splitlines()
        index = next(number for number, line in enumerate(lines) if line.startswith(drop))
        text = "\n".join(lines[:index] + lines[index + 1:])
    path.write_text(text)
    with pytest.raises(BotConfigurationError) as raised:
        load_bot_settings(path)
    assert drop.strip("[]") in str(raised.value)


@pytest.mark.parametrize("change, key", [
    ('internal_url = "http://127.0.0.1:8001"', 'internal_url = "http://127.0.0.1:8001/"'),
    ("read_timeout_seconds = 5", "read_timeout_seconds = 0"),
    ("read_timeout_seconds = 5", "read_timeout_seconds = 13"),
    ("read_timeout_seconds = 5", "read_timeout_seconds = true"),
])
def test_an_unusable_core_value_stops_the_start_naming_the_key(tmp_path, bot_text, change, key):
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(bot_text.replace(change, key, 1))
    with pytest.raises(BotConfigurationError, match="core"):
        load_bot_settings(path)


@pytest.mark.parametrize("content", ["", "\n", "first\nsecond\n", "ключ\n", "with space inside\x01\n"])
def test_a_service_secret_file_without_one_printable_line_stops_the_start(tmp_path, bot_text, secret_file, content):
    from wortly_telegram.config import load_bot_settings
    secret_file.write_text(content)
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    with pytest.raises(BotConfigurationError) as raised:
        load_bot_settings(path)
    assert str(raised.value).startswith("core.service_secret_file")
    assert content.strip() == "" or content.strip() not in str(raised.value)


@pytest.mark.parametrize("content", [
    SERVICE_SECRET[:-1], SERVICE_SECRET + "0", SERVICE_SECRET.upper(), "A" + SERVICE_SECRET[1:],
    "g" + SERVICE_SECRET[1:], "synthetic-service-secret", SERVICE_SECRET[:32] + " " + SERVICE_SECRET[33:],
    SERVICE_SECRET[:32] + "\t" + SERVICE_SECRET[33:], "0x" + SERVICE_SECRET[2:], "ключ" * 16,
])
def test_a_service_secret_that_is_not_64_lowercase_hex_stops_the_start_without_the_value(tmp_path, bot_text,
                                                                                         secret_file, content):
    from wortly_telegram.config import load_bot_settings
    secret_file.write_text(content + "\n")
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    with pytest.raises(BotConfigurationError) as raised:
        load_bot_settings(path)
    message = str(raised.value)
    assert message == KEY_FORMAT_ERROR
    assert content not in message and str(secret_file) not in message


@pytest.mark.parametrize("content", [SERVICE_SECRET + "\n", "\n" + SERVICE_SECRET + "\n", SERVICE_SECRET,
                                     SERVICE_SECRET + "\r\n", "  " + SERVICE_SECRET + " \t\n", "\t" + SERVICE_SECRET])
def test_a_service_secret_of_64_lowercase_hex_is_read_whatever_surrounds_its_line(tmp_path, bot_text, secret_file,
                                                                                  content):
    from wortly_telegram.config import load_bot_settings
    secret_file.write_text(content)
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    assert load_bot_settings(path).core.service_secret == SERVICE_SECRET


@pytest.mark.parametrize("name", ["WORTLY_SERVICE_SECRET", "WORTLY_CORE_SERVICE_SECRET", "SERVICE_SECRET",
                                  "X_WORTLY_SERVICE_SECRET", "CORE_SERVICE_SECRET_FILE"])
def test_the_service_secret_comes_only_from_its_file(tmp_path, bot_text, secret_file, monkeypatch, name):
    from wortly_telegram.config import load_bot_settings
    monkeypatch.setenv(name, "secret-from-the-environment")
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    assert load_bot_settings(path).core.service_secret == SERVICE_SECRET
    # Without a readable file the environment does not stand in for it.
    path.write_text(bot_text.replace(str(secret_file), str(tmp_path / "missing")))
    with pytest.raises(BotConfigurationError, match="core.service_secret_file cannot be read"):
        load_bot_settings(path)


def test_the_bot_loader_reads_secret_paths_only_as_absolute_regular_files(tmp_path, bot_text, secret_file):
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(bot_text.replace(str(secret_file), "service_secret"))
    with pytest.raises(BotConfigurationError, match="core.service_secret_file must be an absolute path"):
        load_bot_settings(path)
    path.write_text(bot_text.replace(str(secret_file), str(tmp_path)))
    with pytest.raises(BotConfigurationError, match="core.service_secret_file is not a regular file"):
        load_bot_settings(path)
    path.write_text(bot_text.replace(str(secret_file), str(tmp_path / "missing")))
    with pytest.raises(BotConfigurationError, match="core.service_secret_file cannot be read"):
        load_bot_settings(path)


def test_a_secret_file_of_another_user_stops_the_start(tmp_path, bot_text, monkeypatch):
    import os
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(BotConfigurationError, match="must be owned by the process user"):
        load_bot_settings(path)


def test_the_example_bot_file_loads_once_its_secret_paths_name_files(tmp_path, token_file, secret_file):
    from wortly_telegram.config import load_bot_settings
    example = (SOURCE.parents[1] / "config" / "bot.example.toml").read_text(encoding="utf-8")
    # Fictitious values only: no table of variant A, no secret in the file.
    import tomllib
    assert set(tomllib.loads(example)) == {"bot", "core", "telegram"}
    assert "synthetic" not in example and "123:" not in example
    path = tmp_path / "bot.toml"
    path.write_text(example.replace("/path/to/wortly/service_secret", str(secret_file))
                    .replace("/path/to/wortly/bot_token", str(token_file)))
    settings = load_bot_settings(path)
    assert settings.core.internal_url == "http://127.0.0.1:8001"
    assert settings.leader.stop_margin > settings.core.request_budget


def test_the_bot_loader_needs_no_core_reader():
    # The core's reader imports psycopg at its top; the bot's loader must not reach it.
    tree = ast.parse((SOURCE / "config.py").read_text(encoding="utf-8"))
    loader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "load_bot_settings")
    imported = [node.module for node in ast.walk(loader) if isinstance(node, ast.ImportFrom)]
    assert imported == []


# An unknown key stops the start wherever it stands: in the root and in every table of each role.
BOT_TABLES = ("bot", "core", "telegram")
DELIVERY_TABLES = ("database", "delivery", "telegram")


def with_unknown_key(text: str, table: str | None) -> str:
    if table is None:
        return "unknown_key = 1\n" + text
    return text.replace(f"[{table}]\n", f"[{table}]\nunknown_key = 1\n", 1)


@pytest.mark.parametrize("table", [None, *BOT_TABLES])
def test_an_unknown_key_anywhere_stops_the_bot(tmp_path, bot_text, table):
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(with_unknown_key(bot_text, table))
    with pytest.raises(BotConfigurationError, match="unknown_key"):
        load_bot_settings(path)


@pytest.mark.parametrize("table", [None, *DELIVERY_TABLES])
def test_an_unknown_key_anywhere_stops_the_delivery(tmp_path, token_file, table):
    pytest.importorskip("psycopg")
    from wortly.operations.config import ConfigurationError
    from wortly_telegram.config import load_delivery_settings
    path = tmp_path / "delivery.toml"
    path.write_text(with_unknown_key(DATABASE + DELIVERY + telegram_table(token_file), table))
    with pytest.raises(ConfigurationError, match="unknown_key"):
        load_delivery_settings(path)


SOURCE = Path(__file__).resolve().parents[1] / "src" / "wortly_telegram"


def key_error(key: str) -> str:
    return f"{key} must be owned by the process user and closed to group and others (mode 0600)"


KEY_ERROR = key_error("telegram.bot_token_file")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_a_token_file_open_to_others_stops_the_delivery_with_the_key_only(tmp_path, token_file, mode):
    pytest.importorskip("psycopg")
    from wortly.operations.config import ConfigurationError
    from wortly_telegram.config import load_delivery_settings
    token_file.chmod(mode)
    path = tmp_path / "delivery.toml"
    path.write_text(DATABASE + DELIVERY + telegram_table(token_file))
    with pytest.raises(ConfigurationError) as raised:
        load_delivery_settings(path)
    message = str(raised.value)
    assert message == KEY_ERROR
    assert "123:secret" not in message and str(token_file) not in message


@pytest.mark.parametrize("which", ["token", "secret"])
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o610, 0o601])
def test_a_secret_file_open_to_others_stops_the_bot_with_the_key_only(tmp_path, token_file, secret_file, bot_text,
                                                                      which, mode):
    from wortly_telegram.config import load_bot_settings
    file, key = ((token_file, "telegram.bot_token_file") if which == "token"
                 else (secret_file, "core.service_secret_file"))
    file.chmod(mode)
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    with pytest.raises(BotConfigurationError) as raised:
        load_bot_settings(path)
    message = str(raised.value)
    assert message == key_error(key)
    assert "123:secret" not in message and SERVICE_SECRET not in message and str(file) not in message


def test_the_mode_is_refused_before_the_format_of_the_service_secret(tmp_path, secret_file, bot_text):
    from wortly_telegram.config import load_bot_settings
    secret_file.write_text("not-a-hex-key\n")
    secret_file.chmod(0o644)
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    with pytest.raises(BotConfigurationError) as raised:
        load_bot_settings(path)
    assert str(raised.value) == key_error("core.service_secret_file")


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o700])
def test_a_secret_file_closed_to_others_is_read(tmp_path, bot_text, secret_file, mode):
    from wortly_telegram.config import load_bot_settings
    secret_file.chmod(mode)
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    assert load_bot_settings(path).core.service_secret == SERVICE_SECRET


def test_the_delivery_entry_exits_with_code_two_on_a_token_file_open_to_others(tmp_path, token_file, capsys):
    pytest.importorskip("psycopg")
    from wortly_telegram.delivery.main import main
    token_file.chmod(0o644)
    path = tmp_path / "role.toml"
    path.write_text(DATABASE + DELIVERY + telegram_table(token_file))
    assert main(["--config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"wortly delivery: configuration error: {KEY_ERROR}\n"
    assert "123:secret" not in captured.err + captured.out


@pytest.mark.parametrize("which", ["token", "secret"])
def test_the_bot_entry_exits_with_code_two_on_a_secret_file_open_to_others(tmp_path, token_file, secret_file,
                                                                           bot_text, capsys, which):
    from wortly_telegram.bot_main import main
    file, key = ((token_file, "telegram.bot_token_file") if which == "token"
                 else (secret_file, "core.service_secret_file"))
    file.chmod(0o644)
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    assert main(["--config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"wortly bot: configuration error: {key_error(key)}\n"
    assert "123:secret" not in captured.err + captured.out
    assert SERVICE_SECRET not in captured.err + captured.out


def test_the_bot_entry_exits_with_code_two_on_a_service_secret_of_the_wrong_format(tmp_path, secret_file, bot_text,
                                                                                   capsys):
    from wortly_telegram.bot_main import main
    wrong = SERVICE_SECRET.upper()
    secret_file.write_text(wrong + "\n")
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    assert main(["--config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.err == f"wortly bot: configuration error: {KEY_FORMAT_ERROR}\n"
    assert wrong not in captured.err + captured.out and str(secret_file) not in captured.err + captured.out


def test_no_module_imports_a_private_name_of_the_core():
    # Private names (leading underscore) are the core's own: it may rename them freely.
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "wortly":
                # A private module path counts as well as a private imported name.
                private_module = any(part.startswith("_") for part in (node.module or "").split("."))
                offenders += [f"{path.name}:{node.lineno} {alias.name}" for alias in node.names
                              if private_module or alias.name.startswith("_")]
    assert offenders == []


_LOAD_WITHOUT_DRIVER = """
import sys
sys.modules["psycopg"] = None
sys.modules["psycopg_pool"] = None
from pathlib import Path
from wortly_telegram.config import load_bot_settings
settings = load_bot_settings(Path(sys.argv[1]))
loaded = sorted(n for n, m in sys.modules.items() if m is not None and n.split(".")[0].startswith("psycopg"))
assert not loaded, loaded
print("loaded", settings.core.internal_url)
"""


def test_the_bot_reads_its_configuration_with_psycopg_blocked(tmp_path, bot_text):
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    result = subprocess.run([sys.executable, "-c", _LOAD_WITHOUT_DRIVER, str(path)], env=probe_environment(),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "loaded http://127.0.0.1:8001" in result.stdout


def test_the_service_secret_rule_is_the_cores_only_one(tmp_path, bot_text, monkeypatch):
    from wortly.operations import config_file
    from wortly_telegram.config import load_bot_settings
    path = tmp_path / "bot.toml"
    path.write_text(bot_text)
    assert load_bot_settings(path).core.service_secret == SERVICE_SECRET
    # Change the rule where the core keeps it: if the bot had a copy, it would not notice.
    monkeypatch.setattr(config_file, "_SERVICE_SECRET_FORMAT", re.compile(r"[0-9a-f]{8}"))
    with pytest.raises(BotConfigurationError) as raised:
        load_bot_settings(path)
    assert str(raised.value) == KEY_FORMAT_ERROR


def test_the_bot_holds_no_copy_of_the_secret_rules():
    source = (Path(__file__).resolve().parents[1] / "src" / "wortly_telegram" / "config.py").read_text("utf-8")
    assert "re.compile" not in source and "[0-9a-f]" not in source
    assert '" " <=' not in source and "_BotTable" not in source and "S_IRWXG" not in source
