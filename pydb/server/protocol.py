"""The wire protocol between PyDB clients and the server.

Every message is a *frame*: a 4-byte big-endian length, then that many bytes of UTF-8 JSON
holding one object. After connecting, the client reads the server's greeting, then sends
requests and reads one response per request, in order.

Greeting (server to client)::

    {"server": "PyDB", "version": "1.0.0", "protocol": 1}

Request::

    {"sql": "SELECT name FROM users WHERE id = ?", "params": [7]}

Response to a statement that ran::

    {"ok": true, "columns": ["name"], "rows": [["ann"]], "rowcount": 1,
     "message": "SELECT 1", "in_transaction": false}

Response to a statement that failed::

    {"ok": false, "error": {"type": "UniqueViolationError", "message": "..."},
     "in_transaction": true}

``in_transaction`` tells the client whether its session has a transaction open after the
statement. Values are JSON numbers, strings, booleans and null; an INT stays an integer and a
FLOAT is always sent with a decimal point, so types survive the round trip.
"""

import json
import struct
from collections.abc import Mapping
from typing import Any

import pydb.errors
from pydb import __version__
from pydb.errors import ProtocolError, PyDBError, SqlSyntaxError
from pydb.execution.executor import Result

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 16 * 1024 * 1024
HEADER = struct.Struct(">I")

Message = dict[str, Any]


def encode_frame(message: Mapping[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"message of {len(payload)} bytes exceeds the {MAX_FRAME_BYTES} limit")
    return HEADER.pack(len(payload)) + payload


def check_length(length: int) -> None:
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {length} bytes exceeds the {MAX_FRAME_BYTES} byte limit")


def decode_payload(payload: bytes) -> Message:
    try:
        message = json.loads(payload.decode())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"frame is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(message, dict):
        raise ProtocolError("frame must hold a JSON object")
    return message


def greeting() -> Message:
    return {"server": "PyDB", "version": __version__, "protocol": PROTOCOL_VERSION}


def check_greeting(message: Message) -> str:
    """Validate a server greeting and return the server's version."""
    if message.get("server") != "PyDB" or message.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError(f"not a PyDB server speaking protocol {PROTOCOL_VERSION}: {message}")
    return str(message.get("version", "?"))


# --------------------------------------------------------------------- requests


def request(sql: str, params: list[object] | tuple[object, ...] | None = None) -> Message:
    message: Message = {"sql": sql}
    if params is not None:
        message["params"] = list(params)
    return message


def parse_request(message: Message) -> tuple[str, list[object] | None]:
    sql = message.get("sql")
    if not isinstance(sql, str):
        raise ProtocolError('request must have a string "sql" field')
    params = message.get("params")
    if params is not None and not isinstance(params, list):
        raise ProtocolError('"params" must be a list')
    return sql, params


# --------------------------------------------------------------------- responses


def result_message(result: Result, in_transaction: bool) -> Message:
    return {
        "ok": True,
        "columns": list(result.columns),
        "rows": [list(row) for row in result.rows],
        "rowcount": result.rowcount,
        "message": result.message,
        "in_transaction": in_transaction,
    }


def error_message(exc: BaseException, in_transaction: bool) -> Message:
    error: Message = {"type": type(exc).__name__, "message": str(exc)}
    if not isinstance(exc, PyDBError):
        error["type"] = "InternalError"
    if isinstance(exc, SqlSyntaxError):
        error.update(detail=exc.detail, line=exc.line, column=exc.column)
    return {"ok": False, "error": error, "in_transaction": in_transaction}


def message_result(message: Message) -> Result:
    """Turn a response into a :class:`Result`, or raise the error it reports."""
    if message.get("ok") is True:
        return Result(
            columns=tuple(message.get("columns", ())),
            rows=[tuple(row) for row in message.get("rows", ())],
            rowcount=int(message.get("rowcount", -1)),
            message=str(message.get("message", "")),
        )
    raise message_error(message)


def message_error(message: Message) -> PyDBError:
    """Rebuild the exception a failed response describes, as the same PyDB error class."""
    error = message.get("error")
    if not isinstance(error, dict):
        return ProtocolError(f"malformed response: {message}")
    text = str(error.get("message", "unknown error"))
    cls = getattr(pydb.errors, str(error.get("type", "")), None)
    if cls is SqlSyntaxError:
        return SqlSyntaxError(
            str(error.get("detail", text)), int(error.get("line", 0)), int(error.get("column", 0))
        )
    if isinstance(cls, type) and issubclass(cls, PyDBError):
        return cls(text)
    return PyDBError(f"server error: {text}")
