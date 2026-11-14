import pytest

from pydb import __version__
from pydb.errors import (
    DeadlockError,
    ProtocolError,
    PyDBError,
    SqlSyntaxError,
    UniqueViolationError,
)
from pydb.execution.executor import Result
from pydb.server import protocol


def roundtrip(message: protocol.Message) -> protocol.Message:
    frame = protocol.encode_frame(message)
    (length,) = protocol.HEADER.unpack(frame[: protocol.HEADER.size])
    assert length == len(frame) - protocol.HEADER.size
    return protocol.decode_payload(frame[protocol.HEADER.size :])


def test_result_roundtrip_keeps_types() -> None:
    result = Result(("a", "b", "c", "d", "e"), [(1, 2.0, "é", True, None)], 1, "SELECT 1")
    back = protocol.message_result(roundtrip(protocol.result_message(result, True)))
    assert back == result
    assert type(back.rows[0][0]) is int and type(back.rows[0][1]) is float


def test_errors_come_back_as_the_same_class() -> None:
    for exc in (UniqueViolationError("dup"), DeadlockError("cycle"), ProtocolError("bad")):
        message = roundtrip(protocol.error_message(exc, False))
        rebuilt = protocol.message_error(message)
        assert type(rebuilt) is type(exc) and str(rebuilt) == str(exc)


def test_syntax_errors_keep_their_position() -> None:
    exc = SqlSyntaxError("expected a statement", 2, 7)
    rebuilt = protocol.message_error(protocol.error_message(exc, False))
    assert isinstance(rebuilt, SqlSyntaxError)
    assert (rebuilt.detail, rebuilt.line, rebuilt.column) == ("expected a statement", 2, 7)


def test_unknown_and_internal_errors_become_pydb_errors() -> None:
    internal = protocol.error_message(ZeroDivisionError("boom"), False)
    assert internal["error"]["type"] == "InternalError"
    rebuilt = protocol.message_error(internal)
    assert type(rebuilt) is PyDBError and "boom" in str(rebuilt)
    forged = {"ok": False, "error": {"type": "SystemExit", "message": "x"}}
    assert type(protocol.message_error(forged)) is PyDBError


@pytest.mark.parametrize("payload", [b"\xff", b"[1, 2]", b"{", b'"text"'])
def test_bad_payloads_are_protocol_errors(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        protocol.decode_payload(payload)


@pytest.mark.parametrize(
    "message", [{}, {"sql": 1}, {"sql": "SELECT 1", "params": "x"}, {"query": "SELECT 1"}]
)
def test_bad_requests(message: protocol.Message) -> None:
    with pytest.raises(ProtocolError):
        protocol.parse_request(message)


def test_request_roundtrip() -> None:
    assert protocol.parse_request(roundtrip(protocol.request("SELECT ?", (1,)))) == (
        "SELECT ?",
        [1],
    )
    assert protocol.parse_request(protocol.request("SELECT 1")) == ("SELECT 1", None)


def test_oversized_frames_are_refused() -> None:
    with pytest.raises(ProtocolError):
        protocol.check_length(protocol.MAX_FRAME_BYTES + 1)


def test_greeting() -> None:
    assert protocol.check_greeting(roundtrip(protocol.greeting())) == __version__
    with pytest.raises(ProtocolError):
        protocol.check_greeting({"server": "other", "protocol": 1})
