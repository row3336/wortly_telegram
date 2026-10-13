"""A capture of the user-path trail for the adapter's tests (R3-03; core: wortly.operations.trail)."""

from __future__ import annotations

import json
import logging

from wortly.operations import trail as trail_module


class TrailLines(logging.Handler):
    """Collects the lines the writer sends to the logger ``wortly.trail``, as a process's stream would get them."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def texts(self) -> list[str]:
        return [record.getMessage() for record in self.records]

    @property
    def parsed(self) -> list[dict]:
        return [json.loads(text) for text in self.texts]

    def events(self, name: str) -> list[dict]:
        return [line for line in self.parsed if line["ev"] == name]

    @property
    def everything(self) -> str:
        return "\n".join(self.texts)


def attach() -> TrailLines:
    handler = TrailLines()
    logging.getLogger(trail_module.LOGGER_NAME).addHandler(handler)
    return handler


def detach(handler: TrailLines) -> None:
    logging.getLogger(trail_module.LOGGER_NAME).removeHandler(handler)
