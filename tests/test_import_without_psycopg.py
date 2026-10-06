"""The adapter imports without a database driver (design T-D §9.2, §9.5; finding Н2).

A separate interpreter blocks psycopg and psycopg_pool before anything is imported. The
handlers, the channel, the classifier and the templates must load; the core's services
are imported only inside the gateway's functions and the process entries.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT.parent / "core" / "src"

MODULES = (
    "wortly_telegram",
    "wortly_telegram.app",
    "wortly_telegram.router",
    "wortly_telegram.start",
    "wortly_telegram.gateway",
    "wortly_telegram.reply",
    "wortly_telegram.webapp",
    "wortly_telegram.config",
    "wortly_telegram.leader",
    "wortly_telegram.logs",
    "wortly_telegram.bot_main",
    "wortly_telegram.service",
    "wortly_telegram.delivery",
    "wortly_telegram.delivery.channel",
    "wortly_telegram.delivery.classify",
    "wortly_telegram.delivery.capacity",
    "wortly_telegram.delivery.main",
    "wortly_telegram.messages",
    "wortly_telegram.messages.registry",
    "wortly_telegram.messages.plural",
    "wortly_telegram.messages.access_approved",
)

_PROBE = """
import importlib, sys
sys.modules["psycopg"] = None
sys.modules["psycopg_pool"] = None
for name in sys.argv[1:]:
    importlib.import_module(name)
from wortly.jobs.kinds import discover as kinds
from wortly_telegram.messages.registry import discover as templates
from wortly_telegram.router import discover as functions
assert templates().missing(kinds().names) == frozenset()
functions()
try:
    importlib.import_module("wortly.jobs.repository")
except ImportError:
    print("driver blocked")
else:
    raise SystemExit("psycopg was not blocked")
loaded = sorted(name for name, module in sys.modules.items()
                if module is not None and name.split(".")[0] in ("psycopg", "psycopg_pool"))
assert not loaded, loaded
print("imported", len(sys.argv) - 1)
"""


def run_probe(*modules: str) -> subprocess.CompletedProcess:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(CORE_SRC)))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-c", _PROBE, *modules], cwd=ROOT, env=environment,
                          capture_output=True, text=True, timeout=120)


def test_adapter_modules_import_without_psycopg():
    result = run_probe(*MODULES)
    assert result.returncode == 0, result.stderr
    assert "driver blocked" in result.stdout
    assert f"imported {len(MODULES)}" in result.stdout
