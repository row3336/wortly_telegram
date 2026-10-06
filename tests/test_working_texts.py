"""Working texts are marked in one way, so the owner finds them all with one grep (finding З9 of QA T1)."""

from __future__ import annotations

from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[1] / "src" / "wortly_telegram"
MARK = "# WORKING TEXT (not set by the specification)"

# Every module that holds a text for the user, and the constant that holds it.
WORKING_TEXTS = (
    ("start.py", "TEXTS"),
    ("webapp.py", "BUTTON_LABELS"),
    ("messages/access_approved.py", "TEXTS"),
    ("messages/inbox_message.py", "HEADINGS"),
)


@pytest.mark.parametrize(("module", "constant"), WORKING_TEXTS)
def test_the_constant_with_a_working_text_follows_the_mark(module, constant):
    lines = (SRC / module).read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"{constant}:"))
    block = start
    # The mark opens the comment block that sits directly above the constant.
    while block > 0 and lines[block - 1].startswith("#"):
        block -= 1
    assert lines[block].startswith(MARK), f"{module}: {constant} is not marked as a working text"
    comment = " ".join(line.removeprefix("# ") for line in lines[block:start])
    assert "final wording waits for the owner" in comment


def test_no_other_module_marks_a_working_text():
    marked = sorted(path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                    if MARK in path.read_text(encoding="utf-8"))
    assert marked == sorted(module for module, _ in WORKING_TEXTS)
