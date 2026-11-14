"""PEP 249 behaviour of pydb.dbapi."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import pydb.dbapi as dbapi


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "api.db"


@pytest.fixture
def conn(path: Path) -> Iterator[dbapi.Connection]:
    connection = dbapi.connect(path)
    connection.execute("CREATE TABLE items (id INT PRIMARY KEY, name TEXT, price FLOAT)")
    yield connection
    connection.close()


def test_module_globals() -> None:
    assert dbapi.apilevel == "2.0"
    assert dbapi.threadsafety == 1
    assert dbapi.paramstyle == "qmark"
    for name in (
        "Warning",
        "Error",
        "InterfaceError",
        "DatabaseError",
        "DataError",
        "OperationalError",
        "IntegrityError",
        "InternalError",
        "ProgrammingError",
        "NotSupportedError",
    ):
        assert issubclass(getattr(dbapi, name), Exception)
        assert getattr(dbapi.Connection, name) is getattr(dbapi, name)
    assert issubclass(dbapi.IntegrityError, dbapi.DatabaseError)
    assert issubclass(dbapi.DatabaseError, dbapi.Error)


def test_insert_query_and_fetch(conn: dbapi.Connection) -> None:
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO items VALUES (?, ?, ?)", [(1, "pen", 1.5), (2, "ink", 4.0), (3, None, 2)]
    )
    assert cur.rowcount == 3
    cur.execute("SELECT id, name, price FROM items ORDER BY id")
    assert [d[0] for d in cur.description or ()] == ["id", "name", "price"]
    assert cur.description is not None
    assert cur.description[0][1] == dbapi.NUMBER
    assert cur.description[1][1] == dbapi.STRING
    assert cur.description[2][1] == "FLOAT"
    assert cur.fetchone() == (1, "pen", 1.5)
    cur.arraysize = 1
    assert cur.fetchmany() == [(2, "ink", 4.0)]
    assert cur.fetchall() == [(3, None, 2.0)]
    assert cur.fetchone() is None
    assert cur.fetchall() == []


def test_cursor_is_iterable(conn: dbapi.Connection) -> None:
    cur = conn.cursor()
    cur.executemany("INSERT INTO items (id) VALUES (?)", [(i,) for i in range(5)])
    assert [row[0] for row in cur.execute("SELECT id FROM items ORDER BY id")] == [0, 1, 2, 3, 4]


def test_changes_are_invisible_to_others_until_commit(conn: dbapi.Connection, path: Path) -> None:
    conn.execute("INSERT INTO items VALUES (1, 'pen', 1.0)")
    assert conn.in_transaction  # opened implicitly before the INSERT
    other = dbapi.connect(path)
    try:
        counted: list[object] = []
        reader = threading.Thread(
            target=lambda: counted.append(other.execute("SELECT COUNT(*) FROM items").fetchone())
        )
        reader.start()
        reader.join(0.3)
        assert reader.is_alive()  # blocked on the uncommitted insert
        conn.commit()
        reader.join(10)
        assert counted == [(1,)]
    finally:
        other.close()


def test_rollback(conn: dbapi.Connection) -> None:
    conn.execute("INSERT INTO items VALUES (1, 'pen', 1.0)")
    conn.rollback()
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone() == (0,)


def test_close_rolls_back_uncommitted_changes(path: Path) -> None:
    conn = dbapi.connect(path)
    conn.execute("CREATE TABLE t (id INT)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.close()
    conn = dbapi.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
    conn.close()


def test_context_manager_commits_or_rolls_back(conn: dbapi.Connection) -> None:
    with conn:
        conn.execute("INSERT INTO items VALUES (1, 'pen', 1.0)")
    assert not conn.in_transaction
    with pytest.raises(ZeroDivisionError), conn:
        conn.execute("INSERT INTO items VALUES (2, 'ink', 1.0)")
        raise ZeroDivisionError
    assert conn.execute("SELECT id FROM items").fetchall() == [(1,)]


def test_autocommit(conn: dbapi.Connection) -> None:
    conn.autocommit = True
    conn.execute("INSERT INTO items VALUES (1, 'pen', 1.0)")
    assert not conn.in_transaction


def test_schema_change_inside_a_transaction_is_refused(conn: dbapi.Connection) -> None:
    conn.execute("INSERT INTO items VALUES (1, 'pen', 1.0)")
    with pytest.raises(dbapi.ProgrammingError, match="COMMIT"):
        conn.execute("CREATE TABLE other (id INT)")
    conn.commit()
    conn.execute("CREATE TABLE other (id INT)")


@pytest.mark.parametrize(
    ("sql", "params", "error"),
    [
        ("INSERT INTO items VALUES (1, 'a', 1.0)", (), dbapi.IntegrityError),
        ("INSERT INTO items VALUES (2, NULL, 'x')", (), dbapi.DataError),
        ("SELECT 1 / 0", (), dbapi.DataError),
        ("SELEC 1", (), dbapi.ProgrammingError),
        ("SELECT * FROM missing", (), dbapi.ProgrammingError),
        ("SELECT ?", (), dbapi.ProgrammingError),
        ("SELECT ?", (b"x",), dbapi.ProgrammingError),
        ("SELECT ?", "x", dbapi.ProgrammingError),
    ],
)
def test_errors_map_to_pep249_classes(
    conn: dbapi.Connection, sql: str, params: tuple[object, ...], error: type[Exception]
) -> None:
    conn.execute("INSERT INTO items VALUES (1, 'a', 1.0)")
    conn.commit()
    with pytest.raises(error) as info:
        conn.execute(sql, params)
    assert isinstance(info.value, dbapi.Error)


def test_fetch_without_a_result_set(conn: dbapi.Connection) -> None:
    cur = conn.execute("INSERT INTO items VALUES (1, 'pen', 1.0)")
    assert cur.description is None
    with pytest.raises(dbapi.ProgrammingError):
        cur.fetchone()


def test_closed_objects_raise_interface_error(conn: dbapi.Connection, path: Path) -> None:
    cur = conn.cursor()
    cur.close()
    with pytest.raises(dbapi.InterfaceError):
        cur.execute("SELECT 1")
    other = dbapi.connect(path)
    other_cur = other.cursor()
    other.close()
    other.close()  # closing twice is fine
    with pytest.raises(dbapi.InterfaceError):
        other.cursor()
    with pytest.raises(dbapi.InterfaceError):
        other_cur.execute("SELECT 1")


def test_connections_to_one_file_share_one_database(path: Path) -> None:
    first = dbapi.connect(path)
    second = dbapi.connect(path)
    assert first._shared is second._shared
    first.close()
    second.execute("CREATE TABLE t (id INT)")  # still open
    second.close()
    assert path not in dbapi._open  # closed with the last connection


def test_unsupported_types() -> None:
    with pytest.raises(dbapi.NotSupportedError):
        dbapi.Date(2026, 1, 1)
    assert dbapi.BINARY != "TEXT"


def test_deadlock_is_an_operational_error(path: Path) -> None:
    one, two = dbapi.connect(path), dbapi.connect(path)
    try:
        one.execute("CREATE TABLE a (id INT)")
        one.execute("CREATE TABLE b (id INT)")
        one.execute("INSERT INTO a VALUES (1)")
        two.execute("INSERT INTO b VALUES (1)")
        blocked = threading.Thread(target=lambda: one.execute("INSERT INTO b VALUES (2)"))
        blocked.start()
        shared = one._shared.db
        while not shared.locks.waiting():
            blocked.join(0.01)
        with pytest.raises(dbapi.OperationalError, match="deadlock"):
            two.execute("INSERT INTO a VALUES (2)")
        blocked.join(10)
        one.commit()
    finally:
        one.close()
        two.close()
