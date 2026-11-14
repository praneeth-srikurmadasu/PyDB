"""An asyncio TCP server: one session per connection, statements run on worker threads.

The event loop only moves frames. Each connection gets a :class:`~pydb.session.Session` and
a single worker thread that runs its statements in order, so a statement waiting for a
table lock blocks that connection's thread, never the loop or the other connections. When a
connection ends, cleanly or not, its session is closed, which rolls back any open
transaction and releases its locks.

The server has no authentication or encryption. It listens on 127.0.0.1 by default; expose
it only on networks you trust.
"""

import asyncio
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import TracebackType
from typing import Self

from pydb.config import DEFAULT_PORT
from pydb.database import Database
from pydb.errors import ProtocolError, PyDBError
from pydb.server import protocol
from pydb.session import Session

DEFAULT_MAX_CONNECTIONS = 64


class Server:
    def __init__(
        self,
        db: Database,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> None:
        self.db = db
        self.host = host
        self.port = port
        self.max_connections = max_connections
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.connections_served = 0

    @property
    def address(self) -> tuple[str, int]:
        """The host and port actually listened on (useful with ``port=0``)."""
        if self._server is None:
            raise RuntimeError("the server is not started")
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, self.host, self.port)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        await self._server.serve_forever()

    async def close(self) -> None:
        """Stop accepting, disconnect every client and wait for their sessions to close."""
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    # ----------------------------------------------------------------- connections

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        if len(self._connections) >= self.max_connections:
            refusal = ProtocolError(f"too many connections (limit {self.max_connections})")
            writer.write(protocol.encode_frame(protocol.error_message(refusal, False)))
            with contextlib.suppress(ConnectionError):
                await writer.drain()
            writer.close()
            return
        self._connections.add(task)
        self._writers.add(writer)
        self.connections_served += 1
        try:
            await self._serve(reader, writer)
        finally:
            self._writers.discard(writer)
            self._connections.discard(task)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pydb-session")
        session: Session | None = None
        try:
            session = await loop.run_in_executor(worker, self.db.session)
            await self._send(writer, protocol.greeting())
            while True:
                try:
                    message = await _read_frame(reader)
                except ProtocolError as exc:
                    await self._send(writer, protocol.error_message(exc, session.in_transaction))
                    return  # the stream can no longer be trusted
                if message is None:
                    return
                response = await loop.run_in_executor(worker, _run, session, message)
                await self._send(writer, response)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass  # the client went away; its session is closed below
        except PyDBError as exc:  # e.g. the database closed while connecting
            with contextlib.suppress(ConnectionError):
                await self._send(writer, protocol.error_message(exc, False))
        finally:
            if session is not None:
                await loop.run_in_executor(worker, session.close)
            worker.shutdown(wait=False)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def _send(self, writer: asyncio.StreamWriter, message: protocol.Message) -> None:
        writer.write(protocol.encode_frame(message))
        await writer.drain()


def _run(session: Session, message: protocol.Message) -> protocol.Message:
    """Run one request on the connection's worker thread."""
    try:
        sql, params = protocol.parse_request(message)
        result = session.execute(sql, params)
    except Exception as exc:  # every failure goes back to the client, typed
        return protocol.error_message(exc, session.in_transaction)
    try:
        return protocol.result_message(result, session.in_transaction)
    except (TypeError, ValueError) as exc:
        return protocol.error_message(exc, session.in_transaction)


async def _read_frame(reader: asyncio.StreamReader) -> protocol.Message | None:
    """Read one frame, or return None if the client closed the connection between frames."""
    try:
        header = await reader.readexactly(protocol.HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise ProtocolError("connection closed inside a frame header") from None
        return None
    (length,) = protocol.HEADER.unpack(header)
    protocol.check_length(length)
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise ProtocolError("connection closed inside a frame") from None
    return protocol.decode_payload(payload)


class BackgroundServer:
    """Run a :class:`Server` on its own thread and event loop, e.g. inside tests or an app.

    ::

        with BackgroundServer(db, port=0) as server:
            host, port = server.address
    """

    def __init__(
        self,
        db: Database,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        *,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
    ) -> None:
        self.server = Server(db, host, port, max_connections=max_connections)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="pydb-server")
        self._started = False

    @property
    def address(self) -> tuple[str, int]:
        return self.server.address

    def start(self) -> tuple[str, int]:
        self._thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self.server.start(), self._loop).result()
        except BaseException:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()
            raise
        self._started = True
        return self.address

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        asyncio.run_coroutine_threadsafe(self.server.close(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
