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
    ("webapp.py", "CARD_BUTTON_LABELS"),
    ("messages/access_approved.py", "TEXTS"),
    ("messages/inbox_message.py", "HEADINGS"),
    ("messages/inbox_message.py", "SENDER_LABELS"),
    ("messages/inbox_message.py", "NUMBER_LABELS"),
    ("messages/access_request.py", "REQUESTS_ACCESS"),
    ("messages/access_request.py", "APPROVE_LABELS"),
    ("messages/access_request.py", "REJECT_LABELS"),
    ("messages/access_request.py", "REJECT_WITH_REASON_LABELS"),
    ("messages/access_reminder.py", "PENDING_DECISION"),
    ("messages/access_reminder.py", "SHOW_REQUESTS_LABELS"),
    ("service/access.py", "APPROVED"),
    ("service/access.py", "REJECTED"),
    ("service/access.py", "ALREADY_HANDLED"),
    ("service/access.py", "NO_PENDING"),
    ("service/access.py", "REASON_PROMPT"),
    ("service/access.py", "REASON_TOO_LONG"),
    ("service/access.py", "REASON_REFUSED"),
    ("service/access.py", "REASON_LINE"),
    ("service/access.py", "FORBIDDEN"),
    ("service/access.py", "TRY_LATER"),
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
    assert marked == sorted({module for module, _ in WORKING_TEXTS})


def test_every_mark_names_one_constant():
    # З9 of QA T1: one mark per constant, so no text hides under another constant's mark.
    for module in {module for module, _ in WORKING_TEXTS}:
        constants = {constant for name, constant in WORKING_TEXTS if name == module}
        marks = (SRC / module).read_text(encoding="utf-8").count(MARK)
        assert marks == len(constants), module
