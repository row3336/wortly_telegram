"""A [database] table a delivery configuration can load since core D1: conninfo names a passfile.

The core refuses a conninfo without an absolute passfile that is a regular file owned by the process user,
closed to group and others (mode 0600) and holding a password line for the connection. The file here is
synthetic: one wildcard line with a placeholder, created with mode 0600 inside the test's tmp directory.
No server is behind it and no test connects.
"""

from __future__ import annotations

import os
from pathlib import Path


def make_passfile(directory: Path) -> Path:
    path = directory / "synthetic.pgpass"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("*:*:*:*:synthetic-placeholder-not-a-secret\n")
    return path


def database_table(passfile: Path) -> str:
    return ('\n[database]\nconninfo = "host=/tmp port=5432 dbname=wortly_test user=wortly sslmode=disable '
            f'passfile={passfile}"\n'
            "connect_timeout_seconds = 5\npool_min_size = 0\npool_max_size = 2\npool_timeout_seconds = 5\n")
