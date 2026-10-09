"""Where the bot's PostgreSQL tests connect: the core's disposable, password-protected stand.

Owner decision R01: tests with a database log in with a password, on the stand only. The
checks are the core's own (core/tests/wortly_test_pg.py): the marker, then host, port,
role, database and password file before connecting, then the server's identity after.
The bot's tests already run on the sibling core checkout (pyproject pythonpath
``../core/src``, the core's docs in test_internal_client), so the module is loaded from
that checkout rather than copied: a check tightened in the core holds here too, and the
checks come from the same tree as the migrations the tests apply.

Anything wrong ends the whole run through pytest.exit, not one test: a missing module or
a refused stand would refuse every database test the same way. There is no fallback to
another server. The module is loaded on first use, so collecting the tests and every test
without a database need neither the stand nor the core's module.
"""

from __future__ import annotations

import functools
import importlib.util
from pathlib import Path
import sys

import pytest


CORE_MODULE = Path(__file__).resolve().parents[2] / "core" / "tests" / "wortly_test_pg.py"
# Not "wortly_test_pg": that is the name the core's own tests import it by.
MODULE_NAME = "wortly_core_test_pg"


def stop(message: str):
    pytest.exit(message, returncode=pytest.ExitCode.INTERRUPTED)


@functools.cache
def core_module(path: Path = CORE_MODULE):
    """The core's stand module, executed once per path; it reads WORTLY_TEST_PG_MARKER now."""
    if not path.is_file():
        stop(f"PostgreSQL test stand refused: the core's checks are missing at {path}; "
             "the run stops, there is no fallback")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    # The module's dataclass looks itself up in sys.modules while it is being defined.
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def connect_params(path: Path = CORE_MODULE) -> dict:
    """Keyword arguments for psycopg.connect to the verified stand."""
    module = core_module(path)
    try:
        return module.connect_params()
    except module.StandRefused as error:
        stop(str(error))  # the core's text already says the run stops
