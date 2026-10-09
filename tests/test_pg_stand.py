"""pg_stand: the bot's database tests reach the core's stand or stop the run (owner decision R01).

No test here opens a database connection: psycopg.connect is replaced by a fake server
that answers the core's identity queries. Each test executes its own copy of the core's
module, so the marker variable is read afresh and no verified state leaks between tests.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

psycopg = pytest.importorskip("psycopg")

import pg_stand  # noqa: E402


TESTS = Path(__file__).resolve().parent
NONCE = "0123456789abcdef"
SERVER = {"cluster_name": f"wortly-probe-{NONCE}", "socket login": True, "listen_addresses": "",
          "port": "55442", "current_user": "wortly_test", "rolsuper": False, "database": "wortly_test"}
DATABASES = ["postgres", "template0", "template1", "wortly_test"]
ROLES = ["wortly_probe_bootstrap", "wortly_test"]


class FakeServer:
    """Answers check_instance's queries; records every login attempt."""

    def __init__(self, *, used_password: bool = True, refuse_login: bool = False, **row) -> None:
        self.row = tuple((SERVER | row).values())
        self.used_password = used_password
        self.refuse_login = refuse_login
        self.logins: list[dict] = []

    def connect(self, **parameters):
        self.logins.append(parameters)
        if self.refuse_login:
            raise psycopg.OperationalError("synthetic: no server")
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.pgconn = type("PGconn", (), {"used_password": server.used_password})()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, query: str):
        if "pg_database" in query:
            row = (DATABASES,)
        elif "pg_roles WHERE rolname !~" in query:
            row = (ROLES,)
        else:
            row = self.server.row
        return type("Cursor", (), {"fetchone": lambda _self: row})()


@pytest.fixture
def stand(tmp_path, monkeypatch):
    """A marker of a serving stand in a fresh directory, selected by the marker variable."""
    work = tmp_path.resolve() / "stand"
    work.mkdir()
    marker = work / "probe-marker.json"
    marker.write_text(json.dumps({
        "nonce": NONCE, "probe_pid": 11, "password_mode": "scram", "started_at": "2026-10-08T20:00:00Z",
        "purpose": "agent-tests", "phase": "serving-agent-tests", "work": str(work),
        "passfiles": [str(work / "libpq-passfile"), str(work / "libpq-passfile-wrong")],
        "server_pid": 12, "server_start_epoch": 1}))
    for name in [name for name in os.environ if name.startswith("PG")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("WORTLY_TEST_PG_MARKER", str(marker))
    return work, marker


@pytest.fixture
def core_copy(tmp_path, monkeypatch):
    """A path of its own for the core's module: core_module executes it once per path."""
    # The copy takes the module's name in sys.modules only for this test.
    monkeypatch.delitem(sys.modules, pg_stand.MODULE_NAME, raising=False)
    path = tmp_path / "core" / "wortly_test_pg.py"
    path.parent.mkdir()
    shutil.copy(pg_stand.CORE_MODULE, path)
    return path


def connect(core_copy, server: FakeServer, monkeypatch):
    monkeypatch.setattr(psycopg, "connect", server.connect)
    return pg_stand.connect_params(core_copy)


def stopped(call) -> str:
    # Caught as BaseException: a skip or an xfail raised instead would otherwise end the
    # test as skipped and hide that the run went on.
    try:
        call()
    except BaseException as error:  # noqa: BLE001
        stop = error
    else:
        pytest.fail("the run was not stopped")
    if not isinstance(stop, pytest.exit.Exception):
        pytest.fail(f"the run was not stopped but ended the test with {type(stop).__name__}")
    assert stop.returncode == pytest.ExitCode.INTERRUPTED
    assert "there is no fallback" in stop.msg
    return stop.msg


def test_the_checks_are_the_core_module_of_the_checkout_the_tests_import():
    import wortly

    assert pg_stand.CORE_MODULE == Path(wortly.__file__).resolve().parents[2] / "tests" / "wortly_test_pg.py"
    assert pg_stand.CORE_MODULE.is_file()


def test_a_verified_stand_gives_its_socket_port_role_and_password_file(stand, core_copy, monkeypatch):
    work, _ = stand
    server = FakeServer()
    parameters = connect(core_copy, server, monkeypatch)
    assert parameters == dict(dbname="wortly_test", host=str(work / "s"), port=55442, user="wortly_test",
                              passfile=str(work / "libpq-passfile"))
    assert "password" not in parameters
    # Verified once per process: later fixtures reuse it without another login.
    assert connect(core_copy, server, monkeypatch) == parameters
    assert len(server.logins) == 1


def test_a_missing_core_module_stops_the_run(tmp_path):
    message = stopped(lambda: pg_stand.connect_params(tmp_path / "absent" / "wortly_test_pg.py"))
    assert "the core's checks are missing" in message


@pytest.mark.parametrize("marker", ["relative/probe-marker.json", "", "/nonexistent/probe-marker.json"])
def test_a_marker_that_is_no_stand_stops_the_run_before_any_login(stand, core_copy, monkeypatch, marker):
    monkeypatch.setenv("WORTLY_TEST_PG_MARKER", marker)
    server = FakeServer()
    stopped(lambda: connect(core_copy, server, monkeypatch))
    assert server.logins == []


def test_a_stand_that_is_not_serving_stops_the_run(stand, core_copy, monkeypatch):
    _, marker = stand
    marker.write_text(marker.read_text().replace("serving-agent-tests", "stopping"))
    server = FakeServer()
    stopped(lambda: connect(core_copy, server, monkeypatch))
    assert server.logins == []


def test_a_pg_variable_stops_the_run_before_any_login(stand, core_copy, monkeypatch):
    monkeypatch.setenv("PGHOST", "/Users/wf/py/wortly/db/run")
    server = FakeServer()
    assert "PGHOST" in stopped(lambda: connect(core_copy, server, monkeypatch))
    assert server.logins == []


def test_an_unreachable_stand_stops_the_run_after_one_attempt(stand, core_copy, monkeypatch):
    work, _ = stand
    server = FakeServer(refuse_login=True)
    message = stopped(lambda: connect(core_copy, server, monkeypatch))
    assert "synthetic" not in message  # the server's text stays out of the log
    # One login, to the stand only: nothing is tried on another socket or port afterwards.
    [login] = server.logins
    assert (login["host"], login["port"], login["user"]) == (str(work / "s"), 55442, "wortly_test")


@pytest.mark.parametrize("row, field", [
    ({"port": "55432"}, "port"),
    ({"current_user": "wf"}, "current_user"),
    ({"database": "postgres"}, "database"),
    ({"rolsuper": True}, "rolsuper"),
    ({"cluster_name": "wortly-probe-ffffffffffffffff"}, "cluster_name"),
    ({"listen_addresses": "localhost"}, "listen_addresses"),
])
def test_a_server_that_is_not_the_stand_stops_the_run(stand, core_copy, monkeypatch, row, field):
    assert field in stopped(lambda: connect(core_copy, FakeServer(**row), monkeypatch))


def test_a_login_without_a_password_stops_the_run(stand, core_copy, monkeypatch):
    assert "without a password" in stopped(lambda: connect(core_copy, FakeServer(used_password=False), monkeypatch))


def test_every_database_test_takes_its_parameters_from_the_stand():
    # The former passwordless server must not come back as a literal (R01).
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        assert "55432" not in text and "db/run" not in text, path.name
        if "psycopg.connect(" in text:
            assert "pg_stand.connect_params()" in text, path.name
