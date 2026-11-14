"""The TCP server and client: sessions per connection, isolation, errors, disconnects."""

import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from pydb.client import Client
from pydb.database import Database
from pydb.errors import (
    DeadlockError,
    ParameterError,
    ProtocolError,
    SqlSyntaxError,
    TransactionError,
    UniqueViolationError,
)
from pydb.execution.executor import Result
from pydb.server import BackgroundServer, protocol


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "server.db")
    yield database
    database.close()


@pytest.fixture
def server(db: Database) -> Iterator[BackgroundServer]:
    with BackgroundServer(db, port=0) as running:
        yield running


def connect(server: BackgroundServer) -> Client:
    host, port = server.address
    return Client(host, port, timeout=30)


def wait_for(condition: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


def test_queries_over_the_network(server: BackgroundServer) -> None:
    with connect(server) as client:
        assert client.server_version == protocol.greeting()["version"]
        client.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT, score FLOAT, ok BOOL)")
        result = client.execute("INSERT INTO t VALUES (?, ?, ?, ?)", [1, "ann", 2.5, True])
        assert result.rowcount == 1 and result.message == "INSERT 1"
        client.execute("INSERT INTO t VALUES (2, NULL, 3, FALSE)")
        result = client.execute("SELECT * FROM t ORDER BY id")
        assert result.columns == ("id", "name", "score", "ok")
        assert result.rows == [(1, "ann", 2.5, True), (2, None, 3.0, False)]


def test_errors_arrive_with_their_types_and_the_connection_survives(
    server: BackgroundServer,
) -> None:
    with connect(server) as client:
        client.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        client.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(UniqueViolationError):
            client.execute("INSERT INTO t VALUES (1)")
        with pytest.raises(SqlSyntaxError) as info:
            client.execute("SELEC 1")
        assert (info.value.line, info.value.column) == (1, 1)
        with pytest.raises(ParameterError):
            client.execute("SELECT ?")
        with pytest.raises(TransactionError):
            client.execute("COMMIT")
        assert client.execute("SELECT COUNT(*) FROM t").rows == [(1,)]


def test_each_connection_has_its_own_transaction(server: BackgroundServer) -> None:
    with connect(server) as a, connect(server) as b:
        a.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        a.execute("INSERT INTO t VALUES (1, 10)")
        a.execute("BEGIN")
        assert a.in_transaction and not b.in_transaction
        a.execute("UPDATE t SET v = 20")
        results: list[Result] = []
        reader = threading.Thread(target=lambda: results.append(b.execute("SELECT v FROM t")))
        reader.start()
        reader.join(0.3)
        assert reader.is_alive()  # b waits for a's lock: no dirty read
        a.execute("COMMIT")
        reader.join(10)
        assert [r.rows for r in results] == [[(20,)]]


def test_disconnecting_mid_transaction_rolls_back_and_frees_locks(
    server: BackgroundServer, db: Database
) -> None:
    with connect(server) as setup:
        setup.execute("CREATE TABLE t (id INT)")
    quitter = connect(server)
    quitter.execute("BEGIN")
    quitter.execute("INSERT INTO t VALUES (1)")
    quitter.close()  # no COMMIT
    wait_for(lambda: server.server.active_connections == 0)
    with connect(server) as other:
        assert other.execute("SELECT COUNT(*) FROM t").rows == [(0,)]
        other.execute("INSERT INTO t VALUES (2)")  # the X lock was released
    assert db.tm.active_transactions == []


def test_deadlock_between_clients(server: BackgroundServer, db: Database) -> None:
    with connect(server) as one, connect(server) as two:
        one.execute("CREATE TABLE a (id INT)")
        one.execute("CREATE TABLE b (id INT)")
        one.execute("BEGIN")
        one.execute("INSERT INTO a VALUES (1)")
        two.execute("BEGIN")
        two.execute("INSERT INTO b VALUES (2)")
        blocked = threading.Thread(target=lambda: one.execute("INSERT INTO b VALUES (1)"))
        blocked.start()
        wait_for(lambda: len(db.locks.waiting()) == 1)
        with pytest.raises(DeadlockError):
            two.execute("INSERT INTO a VALUES (2)")
        assert not two.in_transaction
        blocked.join(10)
        one.execute("COMMIT")
        assert one.execute("SELECT COUNT(*) FROM b").rows == [(1,)]


def test_many_clients_increment_concurrently(server: BackgroundServer) -> None:
    with connect(server) as setup:
        setup.execute("CREATE TABLE counter (n INT)")
        setup.execute("INSERT INTO counter VALUES (0)")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with connect(server) as client:
                for _ in range(20):
                    client.execute("UPDATE counter SET n = n + 1")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors
    with connect(server) as client:
        assert client.execute("SELECT n FROM counter").rows == [(160,)]


def raw_connection(server: BackgroundServer) -> socket.socket:
    sock = socket.create_connection(server.address, timeout=10)
    receive(sock)  # greeting
    return sock


def receive(sock: socket.socket) -> protocol.Message | None:
    data = b""
    while len(data) < protocol.HEADER.size:
        chunk = sock.recv(protocol.HEADER.size - len(data))
        if not chunk:
            return None
        data += chunk
    (length,) = protocol.HEADER.unpack(data)
    payload = b""
    while len(payload) < length:
        payload += sock.recv(length - len(payload))
    return protocol.decode_payload(payload)


@pytest.mark.parametrize(
    "frame",
    [
        protocol.HEADER.pack(5) + b"nope!",  # not JSON
        protocol.HEADER.pack(2) + b"[]",  # not an object
        protocol.HEADER.pack(protocol.MAX_FRAME_BYTES + 1),  # too large
    ],
)
def test_malformed_frames_get_an_error_then_the_connection_closes(
    server: BackgroundServer, frame: bytes
) -> None:
    sock = raw_connection(server)
    sock.sendall(frame)
    reply = receive(sock)
    assert reply is not None and reply["ok"] is False
    assert reply["error"]["type"] == "ProtocolError"
    assert receive(sock) is None  # closed by the server
    sock.close()


def test_bad_request_fields_are_reported_and_the_connection_stays(
    server: BackgroundServer,
) -> None:
    sock = raw_connection(server)
    sock.sendall(protocol.encode_frame({"query": "SELECT 1"}))
    reply = receive(sock)
    assert reply is not None and reply["error"]["type"] == "ProtocolError"
    sock.sendall(protocol.encode_frame(protocol.request("SELECT 1 + 1")))
    reply = receive(sock)
    assert reply is not None and reply["rows"] == [[2]]
    sock.close()


def test_connection_closed_inside_a_frame(server: BackgroundServer, db: Database) -> None:
    sock = raw_connection(server)
    sock.sendall(protocol.HEADER.pack(100) + b"{")
    sock.close()
    wait_for(lambda: server.server.active_connections == 0)
    assert db.sessions == [db._default]  # the session was closed


def test_connection_limit(db: Database) -> None:
    with (
        BackgroundServer(db, port=0, max_connections=1) as server,
        connect(server),
        pytest.raises(ProtocolError, match=r"^too many connections \(limit 1\)$"),
    ):
        connect(server)


def test_stopping_the_server_rolls_back_open_transactions(db: Database) -> None:
    db.execute("CREATE TABLE t (id INT)")
    server = BackgroundServer(db, port=0)
    server.start()
    client = connect(server)
    client.execute("BEGIN")
    client.execute("INSERT INTO t VALUES (1)")
    server.stop()
    assert db.execute("SELECT COUNT(*) FROM t").rows == [(0,)]
    with pytest.raises((ProtocolError, OSError)):
        client.execute("SELECT 1")
    client.close()


def test_client_refuses_a_server_that_is_not_pydb() -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.sendall(protocol.encode_frame({"server": "other"}))
        conn.close()

    thread = threading.Thread(target=fake_server)
    thread.start()
    with pytest.raises(ProtocolError, match="not a PyDB server"):
        Client("127.0.0.1", port, timeout=5)
    thread.join(5)
    listener.close()
