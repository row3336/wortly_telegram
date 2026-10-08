"""The bot process: the polling cycle over the fake getUpdates (design T-D §9.1) and its door to the core.

The leader's poller is PTB's Updater. The process reaches the core only through the
internal API (answer 2.4): no database pool, no psycopg, one client closed on exit.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from conftest import TELEGRAM_SETTINGS
from fakes.core_gateway import FakeCore, core_settings, forbid_network
from fakes.telegram_api import message_update, user
from wortly_telegram.bot_main import UpdaterPoller, _serve
from wortly_telegram.leader import Leader, LeaderConfig


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    yield forbid_network(monkeypatch)


class Store:
    def __init__(self) -> None:
        self.released = []

    async def acquire(self, role, *, holder, duration):
        return "lease"

    async def renew(self, lease, *, duration):
        return lease

    async def release(self, lease):
        self.released.append(lease)
        return True


async def test_the_leader_polls_answers_and_confirms_the_updates_when_it_stops(telegram, make_application):
    application = await make_application()
    await application.start()
    telegram.updates(message_update("/start", from_user=user(5005), update_id=4100))
    stop = asyncio.Event()
    store = Store()
    config = LeaderConfig(holder="bot-1", lease=timedelta(seconds=30), stop_margin=timedelta(seconds=5),
                          standby_retry=timedelta(seconds=1))
    leader = Leader(store, UpdaterPoller(application, poll_timeout=timedelta(seconds=7)), config=config)
    running = asyncio.create_task(leader.run(stop))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if telegram.calls_to("sendMessage"):
            break
    stop.set()
    await asyncio.wait_for(running, timeout=10)
    await application.stop()

    assert len(telegram.calls_to("sendMessage")) == 1
    polls = telegram.calls_to("getUpdates")
    # Spelled out, not read from the constant: a new update type must be a deliberate change (ВП-6).
    assert json.loads(polls[0].params["allowed_updates"]) == ["message", "callback_query", "my_chat_member"]
    assert polls[0].params["timeout"] == "7"
    # The last getUpdates, made while stopping, confirms what was fetched (offset = last id + 1).
    assert polls[-1].params["offset"] == "4101"
    assert store.released == ["lease"]
    calls_after_stop = len(telegram.calls)
    await asyncio.sleep(0.05)
    assert len(telegram.calls) == calls_after_stop


def spy_on_signal_handlers(monkeypatch) -> dict:
    """Record what the process registers for signals instead of registering it with the OS."""
    handlers = {}
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler",
                        lambda number, callback, *args: handlers.setdefault(number, callback))
    return handlers


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGINT])
async def test_the_bot_process_stops_on_a_signal_and_releases_the_lease(monkeypatch, telegram, gateway, number):
    from wortly_telegram import app

    real_make_bot = app.make_bot

    async def handle(request):
        if request.url.path.endswith("/getUpdates"):
            await asyncio.sleep(0.02)  # a long poll that is not instant, as the real one
        return telegram.handle(request)

    monkeypatch.setattr(app, "make_bot", lambda settings: real_make_bot(
        settings, httpx_kwargs={"transport": httpx.MockTransport(handle)}))
    handlers = spy_on_signal_handlers(monkeypatch)
    store = Store()
    settings = SimpleNamespace(telegram=TELEGRAM_SETTINGS, poll_timeout=timedelta(seconds=1),
                               leader=LeaderConfig(holder="bot-1", lease=timedelta(seconds=30),
                                                   stop_margin=timedelta(seconds=5),
                                                   standby_retry=timedelta(seconds=1)))
    task = asyncio.create_task(_serve(settings, gateway=gateway, leases=store))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if telegram.calls_to("getUpdates"):
            break
    assert telegram.calls_to("getUpdates"), "polling started"
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[number]()
    assert await asyncio.wait_for(task, timeout=10) == 0
    assert store.released == ["lease"]
    calls = len(telegram.calls)
    await asyncio.sleep(0.1)
    assert len(telegram.calls) == calls, "nothing is sent or polled after the process ended"


LEADER = LeaderConfig(holder="bot-1", lease=timedelta(seconds=30), stop_margin=timedelta(seconds=5),
                      standby_retry=timedelta(seconds=1))


async def test_the_process_takes_and_gives_back_its_lease_through_the_core_and_closes_the_client(
        monkeypatch, telegram):
    from wortly_telegram import app, bot_main, gateway as gateway_module

    real_make_bot, real_build_gateway = app.make_bot, gateway_module.build_gateway
    core = FakeCore()
    built = []

    def build_gateway(settings):
        built.append(real_build_gateway(settings, transport=core.transport()))
        return built[-1]

    monkeypatch.setattr(app, "make_bot", lambda settings: real_make_bot(
        settings, httpx_kwargs={"transport": telegram.transport()}))
    monkeypatch.setattr(gateway_module, "build_gateway", build_gateway)
    handlers = spy_on_signal_handlers(monkeypatch)
    telegram.updates(message_update("/start", from_user=user(5005), update_id=4100))
    settings = SimpleNamespace(telegram=TELEGRAM_SETTINGS, core=core_settings(), poll_timeout=timedelta(seconds=1),
                               leader=LEADER)
    task = asyncio.create_task(bot_main._run(settings))
    for _ in range(300):
        await asyncio.sleep(0.01)
        if telegram.calls_to("sendMessage"):
            break
    assert telegram.calls_to("sendMessage"), "the update was answered"
    handlers[signal.SIGTERM]()
    assert await asyncio.wait_for(task, timeout=10) == 0
    paths = core.paths()
    # The lease went through the core; the /start answer asked the core for the summary,
    # and the message was noted as a contact.
    assert paths[0] == "/internal/v1/leases/bot/acquire" and paths[-1] == "/internal/v1/leases/bot/release"
    assert {"/internal/v1/contact", "/internal/v1/admission/summary"} <= set(paths)
    assert built[0].client._client.is_closed


_PROBE = """
import asyncio, sys
sys.modules["psycopg"] = None
sys.modules["psycopg_pool"] = None
import httpx
from wortly_telegram import bot_main, gateway

# A transport of its own: the default one would load the CA bundle, which the agents'
# sandbox does not let a process read; the client and everything above it are real.
real_build_gateway = gateway.build_gateway
gateway.build_gateway = lambda core: real_build_gateway(core, transport=httpx.MockTransport(
    lambda request: httpx.Response(204)))
seen = {}

async def serve(settings, *, gateway, leases):
    seen["gateway"], seen["leases"] = type(gateway).__name__, type(leases).__name__
    return 0

bot_main._serve = serve
assert bot_main.main(["--config", sys.argv[1]]) == 0, "main"
assert seen == {"gateway": "HttpCoreGateway", "leases": "HttpLeaseStore"}, seen
loaded = sorted(name for name, module in sys.modules.items() if module is not None and (
    name.split(".")[0] in ("psycopg", "psycopg_pool")
    or name in ("wortly.operations.config", "wortly.operations.connections", "wortly.jobs.repository",
                "wortly.jobs.leases", "wortly.jobs.worker", "wortly.identity.service",
                "wortly.identity.admission_adapter")))
assert not loaded, loaded
print("bot process without a database driver")
"""


def bot_file(tmp_path: Path) -> Path:
    token, secret = tmp_path / "token", tmp_path / "service_secret"
    token.write_text("123:fake\n")
    secret.write_text("0123456789abcdef" * 4 + "\n")
    token.chmod(0o600)
    secret.chmod(0o600)
    path = tmp_path / "bot.toml"
    path.write_text(f"""
[bot]
instance = "bot-1"
leader_lease_seconds = 60
stop_margin_seconds = 15
standby_retry_seconds = 10
poll_timeout_seconds = 25
[core]
internal_url = "http://core.internal.test:8001"
service_secret_file = "{secret}"
connect_timeout_seconds = 2
read_timeout_seconds = 5
[telegram]
bot_token_file = "{token}"
web_app_url = "https://app.example.test/"
connect_timeout_seconds = 1
read_timeout_seconds = 1
write_timeout_seconds = 1
pool_timeout_seconds = 1
""")
    return path


def test_the_bot_process_starts_with_the_database_driver_blocked(tmp_path):
    # The same interpreter, with every path it has (the core's interpreter included):
    # only psycopg is taken away, so a failure is the bot's, not the environment's.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT.parent / "core" / "src"),
                                                 *(entry for entry in sys.path if entry)))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-c", _PROBE, str(bot_file(tmp_path))], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "bot process without a database driver" in result.stdout


def test_no_bot_module_names_a_database_part_of_the_core():
    # The bot's modules import neither the driver nor the core's connection, job or
    # identity services, not even inside a function (the process entry included).
    forbidden = ("psycopg", "psycopg_pool", "wortly.operations.connections", "wortly.operations.config",
                 "wortly.jobs", "wortly.identity.service", "wortly.identity.admission_adapter")
    offenders = []
    for name in ("bot_main.py", "gateway.py", "leader.py", "router.py", "start.py", "app.py", "reply.py"):
        for node in ast.walk(ast.parse((ROOT / "src" / "wortly_telegram" / name).read_text(encoding="utf-8"))):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            offenders += [f"{name}:{node.lineno} {module}" for module in modules
                          if any(module == part or module.startswith(part + ".") for part in forbidden)]
    assert offenders == []


def test_the_service_secret_never_reaches_a_log_line_of_the_process(tmp_path, monkeypatch, capsys):
    import logging

    from wortly_telegram import bot_main, logs

    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    def serve(settings):
        # Whatever a library or the bot would write with the secret in it.
        logging.getLogger("httpx").warning("header %s", settings.core.service_secret)
        logging.getLogger("wortly_telegram.probe").warning("x-wortly-service-secret: %s",
                                                           settings.core.service_secret)
        return 0

    root, handler = logging.getLogger(), Capture()
    levels = {name: logging.getLogger(name).level for name in logs.LIBRARY_LOGGERS}
    root.addHandler(handler)
    monkeypatch.setattr(bot_main, "serve", serve)
    try:
        assert bot_main.main(["--config", str(bot_file(tmp_path))]) == 0
    finally:
        root.removeHandler(handler)
        for name, value in levels.items():
            logging.getLogger(name).setLevel(value)
    assert len(lines) == 2
    captured = capsys.readouterr()
    assert not [text for text in (*lines, captured.out, captured.err) if "0123456789abcdef" in text]
