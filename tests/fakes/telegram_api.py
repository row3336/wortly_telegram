"""The Telegram Bot API replaced at PTB's HTTP transport (design T-D §9.1).

The real telegram.Bot builds every request and parses every answer; only the socket is
replaced by ``httpx.MockTransport``. No network, no port. The fake records each call
(method and form fields as sent) and answers from a per-method script: an ``ok``
result, a Bot API error with its code and parameters, or an httpx exception raised
where the transport would raise it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
import itertools
import json
from typing import Any
from urllib.parse import parse_qsl

import httpx
from telegram import Bot
from telegram.request import HTTPXRequest


# A fictitious token in the Bot API form "<bot id>:<secret>" (design §9.1).
TOKEN = "123456789:TEST-fake_secret-PART"
BOT_ID = 123456789
WEB_APP_URL = "https://app.example.test/wortly/"


@dataclass(frozen=True)
class Call:
    method: str
    params: dict[str, str]
    url: str = field(repr=False)

    def json(self, name: str) -> Any:
        return json.loads(self.params[name])


@dataclass(frozen=True)
class _Answer:
    status: int = 200
    body: dict | None = None
    error: Exception | None = None


def _ok(result: Any) -> _Answer:
    return _Answer(200, {"ok": True, "result": result})


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._script: dict[str, deque[_Answer]] = defaultdict(deque)
        self._message_ids = itertools.count(1000)
        # Runs inside the transport while a request is "in flight" (integration tests
        # read the database at that moment).
        self.on_request: Callable[[Call], None] | None = None

    # -- scripting ----------------------------------------------------------------

    def answer(self, method: str, result: Any) -> None:
        self._script[method].append(_ok(result))

    def fail(self, method: str, code: int, description: str = "error", *,
             retry_after: int | None = None, migrate_to_chat_id: int | None = None) -> None:
        body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
        parameters = {}
        if retry_after is not None:
            parameters["retry_after"] = retry_after
        if migrate_to_chat_id is not None:
            parameters["migrate_to_chat_id"] = migrate_to_chat_id
        if parameters:
            body["parameters"] = parameters
        self._script[method].append(_Answer(code, body))

    def raise_error(self, method: str, error: Exception) -> None:
        self._script[method].append(_Answer(error=error))

    def raw(self, method: str, status: int, content: bytes) -> None:
        self._script[method].append(_Answer(status, {"__raw__": content.decode("latin-1")}))

    def updates(self, *batch: dict) -> None:
        """Script one getUpdates answer carrying ``batch``."""
        self.answer("getUpdates", list(batch))

    # -- inspection ---------------------------------------------------------------

    def calls_to(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]

    # -- transport ----------------------------------------------------------------

    def _default(self, method: str, params: dict[str, str]) -> Any:
        if method == "getMe":
            return {"id": BOT_ID, "is_bot": True, "first_name": "Wortly", "username": "wortly_test_bot",
                    "can_join_groups": False, "can_read_all_group_messages": False,
                    "supports_inline_queries": False}
        if method == "sendMessage":
            return {"message_id": next(self._message_ids), "date": 0,
                    "chat": {"id": int(params["chat_id"]), "type": "private"},
                    "text": params.get("text", "")}
        if method == "getUpdates":
            return []
        return True

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = dict(parse_qsl(request.content.decode(), keep_blank_values=True)) if request.content else {}
        call = Call(method, params, str(request.url))
        self.calls.append(call)
        if self.on_request is not None:
            self.on_request(call)
        script = self._script.get(method)
        answer = script.popleft() if script else _ok(self._default(method, params))
        if answer.error is not None:
            raise answer.error
        if answer.body is not None and "__raw__" in answer.body:
            return httpx.Response(answer.status, content=answer.body["__raw__"].encode("latin-1"))
        return httpx.Response(answer.status, json=answer.body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def request(self) -> HTTPXRequest:
        return HTTPXRequest(httpx_kwargs={"transport": self.transport()})

    def bot(self, token: str = TOKEN) -> Bot:
        return Bot(token, request=self.request(), get_updates_request=self.request())


# -- updates as Telegram sends them ---------------------------------------------------

_update_ids = itertools.count(1)


def user(user_id: int, *, first_name: str = "Anna", last_name: str | None = None,
         username: str | None = None, language_code: str | None = None) -> dict:
    data: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": first_name}
    if last_name is not None:
        data["last_name"] = last_name
    if username is not None:
        data["username"] = username
    if language_code is not None:
        data["language_code"] = language_code
    return data


def message_update(text: str, *, from_user: dict, chat_id: int | None = None, message_id: int = 1,
                   update_id: int | None = None, chat_type: str = "private",
                   edited: bool = False) -> dict:
    chat_id = from_user["id"] if chat_id is None else chat_id
    message: dict[str, Any] = {"message_id": message_id, "date": 0, "text": text, "from": from_user,
                               "chat": {"id": chat_id, "type": chat_type}}
    if chat_type == "private":
        message["chat"]["first_name"] = from_user["first_name"]
    if text.startswith("/"):
        command = text.split()[0]
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
    if edited:
        message["edit_date"] = 1
    key = "edited_message" if edited else "message"
    return {"update_id": next(_update_ids) if update_id is None else update_id, key: message}


def callback_update(data: str, *, from_user: dict, callback_id: str = "777", chat_id: int | None = None,
                    message_id: int = 1, update_id: int | None = None) -> dict:
    chat_id = from_user["id"] if chat_id is None else chat_id
    return {"update_id": next(_update_ids) if update_id is None else update_id,
            "callback_query": {"id": callback_id, "from": from_user, "chat_instance": "ci", "data": data,
                               "message": {"message_id": message_id, "date": 0, "text": "old",
                                           "chat": {"id": chat_id, "type": "private"},
                                           "from": {"id": BOT_ID, "is_bot": True, "first_name": "Wortly"}}}}
