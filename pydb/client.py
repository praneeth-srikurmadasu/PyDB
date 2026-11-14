"""A client for the PyDB server.

::

    from pydb.client import Client

    with Client("127.0.0.1", 5433) as client:
        client.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
        client.execute("INSERT INTO t VALUES (?, ?)", [1, "ann"])
        print(client.execute("SELECT * FROM t").rows)

Errors raised by the server arrive as the same exception classes the embedded engine raises
(``UniqueViolationError``, ``DeadlockError`` and so on). Each client is one session: a
``BEGIN`` sent on it opens a transaction that lasts until ``COMMIT`` or ``ROLLBACK``, or until
the connection closes, which rolls it back.
"""

import socket
from collections.abc import Sequence
from types import TracebackType
from typing import Self

from pydb.config import DEFAULT_PORT
from pydb.errors import ProtocolError
from pydb.execution.executor import Result
from pydb.server import protocol


class Client:
    def __init__(
        self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *, timeout: float | None = None
    ) -> None:
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self.closed = False
        self.in_transaction = False
        try:
            greeting = self._receive()
            if greeting.get("ok") is False:  # refused, e.g. too many connections
                raise protocol.message_error(greeting)
            self.server_version = protocol.check_greeting(greeting)
        except BaseException:
            self._sock.close()
            raise

    def execute(self, sql: str, params: Sequence[object] | None = None) -> Result:
        """Run one statement on the server. Each ``?`` takes the next value of ``params``."""
        if self.closed:
            raise ProtocolError("the client is closed")
        self._sock.sendall(
            protocol.encode_frame(protocol.request(sql, None if params is None else list(params)))
        )
        response = self._receive()
        self.in_transaction = bool(response.get("in_transaction", False))
        return protocol.message_result(response)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._sock.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _receive(self) -> protocol.Message:
        (length,) = protocol.HEADER.unpack(self._read_exactly(protocol.HEADER.size))
        protocol.check_length(length)
        return protocol.decode_payload(self._read_exactly(length))

    def _read_exactly(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = self._sock.recv(min(remaining, 1 << 20))
            if not chunk:
                self.close()
                raise ProtocolError("the server closed the connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
