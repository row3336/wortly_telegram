"""Logs of the bot and delivery roles carry no token, message text, Telegram name or initData (TC:273).

The processes run inside logs.private_logs: the core's record factory replaces the
token in every written form and the connection strings, and library loggers stay at
WARNING. The scenario drives the real handlers, the channel, the reply wrapper and the
leader through their failure paths while the root logger records everything at DEBUG.
A control run without the library levels proves the scenario would reveal a leak.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from conftest import TELEGRAM_SETTINGS, feed
from fakes.jobs import envelope, snapshot
from fakes.telegram_api import TOKEN, callback_update, message_update, user
from wortly_telegram import logs
from wortly_telegram.app import make_bot
from wortly_telegram.delivery.capacity import TelegramCapacity
from wortly_telegram.delivery.channel import TelegramChannel
from wortly_telegram.delivery.main import loop_runner
from wortly_telegram.gateway import principal_of
from wortly_telegram.leader import Leader, LeaderConfig
from wortly_telegram.messages.registry import discover
from wortly_telegram.router import ServiceFunction


USER_ID = 731942
NAMES = ("Ivanna", "Secretova", "ivanna_private")
TEXT = "моя приватная фраза 4815"
INIT_DATA = "query_id=AAHdF6IQAAAAAN0XohDhrOrc&user=%7B%22id%22%3A731942%7D&auth_date=1700000000&hash=c501b71e775f74ce"
CONNINFO = "host=db.internal port=5432 dbname=wortly user=wortly_bot sslmode=verify-full"
DELIVERED = "Доступ підтверджено"

FORBIDDEN = (TOKEN, quote(TOKEN, safe=""), TOKEN.partition(":")[2], CONNINFO, *NAMES, TEXT, "query_id=",
             "c501b71e775f74ce", DELIVERED, str(USER_ID))


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.lines.append(self.format(record))


@pytest.fixture
def capture():
    root = logging.getLogger()
    handler, level = Capture(), root.level
    levels = {name: logging.getLogger(name).level for name in logs.LIBRARY_LOGGERS}
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield handler
    root.removeHandler(handler)
    root.setLevel(level)
    for name, value in levels.items():
        logging.getLogger(name).setLevel(value)


def leaking(text: str) -> list[str]:
    return [secret for secret in FORBIDDEN if secret in text]


async def scenario(telegram, gateway, make_application) -> None:
    sender = user(USER_ID, first_name=NAMES[0], last_name=NAMES[1], username=NAMES[2], language_code="uk")

    async def failing(update, context):
        principal_of(update)
        raise RuntimeError(f"handler failed for {TOKEN} with {TEXT} from {NAMES[0]}")

    application = await make_application(functions=(ServiceFunction("probe", ("probe",), "probe", "anyone", failing),))
    # The poller's getUpdates answer carries the user's names and text.
    telegram.updates(message_update(TEXT, from_user=sender, message_id=9))
    await application.bot.get_updates(timeout=0)
    # The core fails with a message full of secrets: only the class may be logged.
    gateway.error = RuntimeError(f"{CONNINFO} {TOKEN} {NAMES[0]}")
    await feed(application, message_update("/start", from_user=sender))
    gateway.error = None
    await feed(application, message_update(TEXT, from_user=sender, message_id=2))
    await feed(application, message_update(INIT_DATA, from_user=sender, message_id=3))
    await feed(application, callback_update(f"probe:{TEXT}", from_user=sender))
    # Telegram's answer quotes the user's text; the reply may have arrived, so it is logged.
    telegram.fail("sendMessage", 400, f"Bad Request: can't parse entities in '{TEXT}'")
    await feed(application, message_update("hello", from_user=sender, message_id=4))
    telegram.raise_error("sendMessage", httpx.ReadTimeout(f"timed out on /bot{TOKEN}/sendMessage", request=None))
    await feed(application, message_update("again", from_user=sender, message_id=5))

    # The delivery channel, called from a thread as in the delivery process.
    bot = make_bot(TELEGRAM_SETTINGS, httpx_kwargs={"transport": telegram.transport()})
    await bot.initialize()
    stop = asyncio.Event()
    channel = TelegramChannel(bot, templates=discover(), web_app_url=TELEGRAM_SETTINGS.web_app_url,
                              capacity=TelegramCapacity(10, wait=lambda seconds: False),
                              run=loop_runner(asyncio.get_running_loop()))
    facts = envelope(language="uk", telegram_user_id=str(USER_ID))
    await asyncio.to_thread(channel.deliver, snapshot(facts=facts))
    telegram.raise_error("sendMessage", httpx.ReadTimeout(f"/bot{quote(TOKEN, safe='')}/sendMessage", request=None))
    await asyncio.to_thread(channel.deliver, snapshot(facts=facts))
    telegram.fail("sendMessage", 403, f"Forbidden: user {NAMES[0]} blocked the bot")
    await asyncio.to_thread(channel.deliver, snapshot(facts=facts))
    telegram.raise_error("sendMessage", httpx.ConnectError(f"cannot reach api.telegram.org/bot{TOKEN}", request=None))
    await asyncio.to_thread(channel.deliver, snapshot(facts=facts))
    await asyncio.to_thread(channel.deliver, snapshot(facts=envelope(language="de")))
    await bot.shutdown()

    # The leader's lease fails with a driver message that names the connection.
    class Store:
        def acquire(self, role, *, holder, duration):
            stop.set()
            raise ConnectionError(f"connection failed: {CONNINFO}")

    leader = Leader(Store(), None, config=LeaderConfig("bot-1", timedelta(seconds=60), timedelta(seconds=20),
                                                       timedelta(seconds=10)))
    await leader.run(stop)

    # Libraries that write the token into URLs, in both written forms.
    logging.getLogger("httpx").warning("HTTP Request: POST https://api.telegram.org/bot%s/getMe", TOKEN)
    logging.getLogger("telegram.ext.Updater").warning("GET /bot%s/getUpdates", quote(TOKEN, safe=""))


async def test_no_log_line_carries_a_secret_a_text_or_a_name(telegram, gateway, make_application, capture):
    with logs.private_logs((*logs.token_forms(TOKEN), CONNINFO)):
        await scenario(telegram, gateway, make_application)
    assert any(record.name.startswith("wortly_telegram") for record in capture.records), "the scenario logged"
    for record, line in zip(capture.records, capture.lines):
        assert not leaking(line), (record.name, leaking(line))
        # Nothing hides in the arguments or the exception either.
        assert not leaking(repr(record.args)) and not leaking(str(record.exc_text or ""))


async def test_control_without_quiet_libraries_the_scenario_reveals_texts(telegram, gateway, make_application,
                                                                         capture):
    # The same scenario with the libraries left at DEBUG: PTB writes the updates it got
    # and the parameters it sends, so the user's text and names, the delivered text and
    # the Telegram ID reach the log. The main test is therefore not vacuous.
    from wortly.operations.log_privacy import private_records

    for name in logs.LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    with private_records(logs.token_forms(TOKEN)):
        await scenario(telegram, gateway, make_application)
    revealed = {secret for line in capture.lines for secret in leaking(line)}
    assert {TEXT, NAMES[0], NAMES[2], DELIVERED, str(USER_ID)} <= revealed
    # The token is still replaced in every form, even at DEBUG.
    assert not revealed & set(logs.token_forms(TOKEN))


def test_token_forms_cover_the_raw_the_encoded_and_the_secret_part():
    raw, encoded, secret = logs.token_forms(TOKEN)
    assert raw == TOKEN and "%3A" in encoded and ":" not in secret and secret in encoded


def test_library_loggers_are_kept_at_warning(capture):
    logs.quiet_libraries()
    for name in ("httpx", "httpcore", "telegram", "telegram.ext", "telegram.Bot"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def test_the_process_entries_run_inside_private_logs(tmp_path, monkeypatch, capture):
    pytest.importorskip("psycopg")
    from wortly_telegram import bot_main
    from wortly_telegram.delivery import main as delivery_main

    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    telegram_table = (f'[telegram]\nbot_token_file = "{token_file}"\nweb_app_url = "https://app.example.test/"\n'
                      "connect_timeout_seconds = 1\nread_timeout_seconds = 1\nwrite_timeout_seconds = 1\n"
                      "pool_timeout_seconds = 1\n")
    database = ('[database]\nconninfo = "host=/tmp port=5432 dbname=wortly_test user=wortly sslmode=disable"\n'
                "connect_timeout_seconds = 5\npool_min_size = 0\npool_max_size = 1\npool_timeout_seconds = 5\n")
    bot = tmp_path / "bot.toml"
    bot.write_text(database + "[bot]\ninstance = \"bot-1\"\nleader_lease_seconds = 60\nstop_margin_seconds = 15\n"
                   "standby_retry_seconds = 10\npoll_timeout_seconds = 25\nlock_timeout_milliseconds = 1000\n"
                   "[access]\nsession_lifetime_seconds = 3600\n" + telegram_table)
    delivery = tmp_path / "delivery.toml"
    delivery.write_text(database + "[delivery]\ninstance = \"delivery-1\"\njob_lease_seconds = 30\n"
                        "recovery_period_seconds = 60\npoll_interval_seconds = 1\nmax_failed_preflights = 3\n"
                        "nondelivery_delay_seconds = 600\nlock_timeout_milliseconds = 1000\nmessages_per_second = 10\n"
                        + telegram_table)

    def serve(settings):
        logging.getLogger("wortly_telegram.probe").warning("token %s and %s", settings.telegram.bot_token,
                                                           quote(settings.telegram.bot_token, safe=""))
        logging.getLogger("httpx").info("HTTP Request: GET /bot%s/getMe", settings.telegram.bot_token)
        return 0

    monkeypatch.setattr(bot_main, "serve", serve)
    monkeypatch.setattr(delivery_main, "serve", serve)
    assert bot_main.main(["--config", str(bot)]) == 0
    assert delivery_main.main(["--config", str(delivery)]) == 0
    assert len([line for line in capture.lines if "wortly_telegram.probe" in line]) == 2
    assert not [line for line in capture.lines if leaking(line)]


def test_log_secrets_hold_the_token_forms_and_both_connection_strings(capture):
    pytest.importorskip("psycopg")
    from wortly.operations.connections import writer_conninfo

    database = SimpleNamespace(conninfo=CONNINFO, connect_timeout_seconds=5)
    secrets = logs.log_secrets(TOKEN, database)
    assert secrets == (*logs.token_forms(TOKEN), CONNINFO, writer_conninfo(database))
    # The pool opens connections with the second form, so drivers may quote it.
    assert writer_conninfo(database) != CONNINFO
    with logs.private_logs(secrets):
        logging.getLogger("wortly_telegram.probe").warning("connection failed: %s", writer_conninfo(database))
    assert [line for line in capture.lines if "wortly_telegram.probe" in line]
    assert not [line for line in capture.lines if "target_session_attrs" in line or "db.internal" in line]


def test_the_adapter_logs_only_classes_and_outcomes():
    # An exception's own text can hold anything (a name, a text, an address), so a logger call
    # takes a constant message and arguments that are class names or outcomes. Nothing is
    # formatted into the message itself (f-string, %, .format), and logger.exception is out:
    # it would attach the traceback and the exception text the call site did not choose.
    # The only two calls that pass an error object are the start errors, whose messages name
    # kinds and modules (registry and router errors), never data; they are listed one by one.
    source = Path(__file__).resolve().parents[1] / "src" / "wortly_telegram"
    start_errors = {
        ("bot_main.py", "bot not started: %s", ("error",)),
        ("delivery/main.py", "delivery not started: %s: %s", ("type(error).__name__", "error")),
    }
    suspicious = []
    for path in source.rglob("*.py"):
        name = path.relative_to(source).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
                continue
            where = (name, node.lineno)
            message = node.args[0] if node.args else None
            if node.func.attr == "exception" or node.keywords:
                suspicious.append((*where, "exception or keyword argument"))
            if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
                suspicious.append((*where, "the message is not a plain string constant"))
                continue
            arguments = tuple(ast.unparse(argument) for argument in node.args[1:])
            if (name, message.value, arguments) in start_errors:
                continue
            for text in arguments:
                if not ((text.startswith("type(") and text.endswith(").__name__"))
                        or text in ("result.outcome", "result.reason or '-'")):
                    suspicious.append((*where, text))
    assert suspicious == []
