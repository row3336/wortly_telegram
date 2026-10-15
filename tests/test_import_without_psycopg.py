"""The adapter imports without a database driver (design T-D §9.2, §9.5; finding Н2).

A separate interpreter blocks psycopg and psycopg_pool before anything is imported. The
handlers, the channel, the classifier and the templates must load; the core's services
are imported only inside the gateway's functions and the process entries.
"""

from __future__ import annotations

import ast
import os
import importlib.util
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
    "wortly_telegram.trail",
    "wortly_telegram.bot_main",
    "wortly_telegram.service",
    "wortly_telegram.service.card_editor",
    "wortly_telegram.card_view",
    "wortly_telegram.echo",
    "wortly_telegram.delivery",
    "wortly_telegram.delivery.channel",
    "wortly_telegram.delivery.classify",
    "wortly_telegram.delivery.capacity",
    "wortly_telegram.delivery.main",
    "wortly_telegram.messages",
    "wortly_telegram.messages.registry",
    "wortly_telegram.messages.plural",
    "wortly_telegram.messages.access_approved",
    # The core module the bot reads its configuration with: it must not need the driver.
    "wortly.operations.config_file",
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
# The core's services are the gateway's and the process entries' business: a module that
# merely imports the handlers must not pull them in (blocking psycopg alone would not show it).
for name in ("wortly.identity.admission_adapter", "wortly.identity.service"):
    assert name not in sys.modules, name
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


def _site_packages_of_telegram_library() -> str:
    # The probe clears PYTHONPATH, so an interpreter that does not carry python-telegram-bot
    # itself (the core's, run with the bot's site-packages on PYTHONPATH) would not find it.
    # Naming the directory it lives in keeps the check as strict: psycopg is blocked in the
    # probe anyway, whatever that directory holds.
    spec = importlib.util.find_spec("telegram")
    assert spec is not None and spec.submodule_search_locations, "python-telegram-bot is not importable"
    return str(Path(next(iter(spec.submodule_search_locations))).parent)


def probe_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(ROOT / "src"), str(CORE_SRC), _site_packages_of_telegram_library()))
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_probe(*modules: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", _PROBE, *modules], cwd=ROOT, env=probe_environment(),
                          capture_output=True, text=True, timeout=120)


def test_adapter_modules_import_without_psycopg():
    result = run_probe(*MODULES)
    assert result.returncode == 0, result.stderr
    assert "driver blocked" in result.stdout
    assert f"imported {len(MODULES)}" in result.stdout


def test_the_gateway_imports_no_core_service_at_module_level():
    tree = ast.parse((ROOT / "src" / "wortly_telegram" / "gateway.py").read_text(encoding="utf-8"))
    imported = [node.module for node in tree.body if isinstance(node, ast.ImportFrom)]
    # Only the principal, a plain value type, may come from the core at module level.
    assert [module for module in imported if module and module.startswith("wortly.")] == [
        "wortly.identity.principal"]
