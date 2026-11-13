"""SQL statements end to end through Database.execute."""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.catalog.integrity import check_table
from pydb.database import Database
from pydb.errors import (
    CatalogError,
    DivisionByZeroError,
    DuplicateObjectError,
    NotNullViolationError,
    NotSupportedError,
    TypeMismatchError,
    UniqueViolationError,
    ValueTooLargeError,
)
from pydb.storage.tuple import Row


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    with Database(tmp_path / "dml.db", pool_size=16) as database:
        yield database


def rows(db: Database, sql: str) -> list[Row]:
    return db.execute(sql).rows


def consistent(db: Database, table: str) -> int:
    return check_table(db.catalog.get_table(table))


USERS = (
    "CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(8) NOT NULL, "
    "email TEXT UNIQUE, age INT DEFAULT 18, active BOOL DEFAULT TRUE)"
)


def test_create_insert_select(db: Database) -> None:
    assert db.execute(USERS).message == "CREATE TABLE"
    result = db.execute(
        "INSERT INTO users (id, name, email) VALUES (1, 'ann', 'a@x'), (2, 'bob', NULL)"
    )
    assert (result.rowcount, result.message) == (2, "INSERT 2")
    assert rows(db, "SELECT * FROM users") == [
        (1, "ann", "a@x", 18, True),
        (2, "bob", None, 18, True),
    ]
    assert rows(db, "SELECT name, age + 1 AS next FROM users WHERE id = 2") == [("bob", 19)]
    assert db.execute("SELECT id AS x FROM users").columns == ("x",)
    assert consistent(db, "users") == 2


def test_insert_without_column_list_and_int_to_float(db: Database) -> None:
    db.execute("CREATE TABLE m (a INT, b FLOAT)")
    db.execute("INSERT INTO m VALUES (1, 2), (-3, 0.5)")
    assert rows(db, "SELECT * FROM m") == [(1, 2.0), (-3, 0.5)]


def test_select_without_from(db: Database) -> None:
    assert rows(db, "SELECT 1 + 2 * 3, 'a' || 'b', 7 / 2, -7 % 2, ABS(-4)") == [(7, "ab", 3, -1, 4)]
    with pytest.raises(DivisionByZeroError):
        db.execute("SELECT 1 / 0")


def test_constraints(db: Database) -> None:
    db.execute(USERS)
    db.execute("INSERT INTO users (id, name, email) VALUES (1, 'ann', 'a@x')")
    cases = [
        ("INSERT INTO users (id, name) VALUES (1, 'dup')", UniqueViolationError, "users_pkey"),
        (
            "INSERT INTO users (id, name, email) VALUES (2, 'b', 'a@x')",
            UniqueViolationError,
            "users_email_key",
        ),
        ("INSERT INTO users (id) VALUES (3)", NotNullViolationError, '"name"'),
        ("INSERT INTO users (id, name) VALUES (NULL, 'x')", NotNullViolationError, '"id"'),
        ("INSERT INTO users (id, name) VALUES (4, 'ninechars')", ValueTooLargeError, "VARCHAR(8)"),
        ("INSERT INTO users (id, name, age) VALUES (5, 'x', 'old')", TypeMismatchError, '"age"'),
        ("UPDATE users SET name = NULL", NotNullViolationError, '"name"'),
    ]
    for sql, error, message in cases:
        with pytest.raises(error, match=re.escape(message)):
            db.execute(sql)
    assert rows(db, "SELECT id FROM users") == [(1,)]
    assert consistent(db, "users") == 1


def test_unique_allows_many_nulls(db: Database) -> None:
    db.execute(USERS)
    db.execute("INSERT INTO users (id, name) VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    assert rows(db, "SELECT COALESCE(email, 'none') FROM users") == [("none",)] * 3


def test_failed_multi_row_insert_changes_nothing(db: Database) -> None:
    db.execute(USERS)
    db.execute("INSERT INTO users (id, name) VALUES (1, 'a')")
    with pytest.raises(UniqueViolationError):
        db.execute("INSERT INTO users (id, name) VALUES (2, 'b'), (3, 'c'), (1, 'dup'), (4, 'd')")
    assert rows(db, "SELECT id FROM users") == [(1,)]
    assert consistent(db, "users") == 1


def test_failed_update_changes_nothing(db: Database) -> None:
    db.execute(USERS)
    db.execute(
        "INSERT INTO users (id, name, email) VALUES (1, 'a', 'x'), (2, 'b', 'y'), (3, 'c', 'z')"
    )
    with pytest.raises(UniqueViolationError):
        db.execute("UPDATE users SET email = 'same'")  # the second row collides with the first
    assert rows(db, "SELECT email FROM users") == [("x",), ("y",), ("z",)]
    assert consistent(db, "users") == 3


def test_update_keeps_indexes_in_sync(db: Database) -> None:
    db.execute(USERS)
    db.execute("CREATE INDEX users_age ON users (age)")
    db.execute("INSERT INTO users (id, name, age) VALUES (1, 'a', 20), (2, 'b', 30), (3, 'c', 40)")
    assert db.execute("UPDATE users SET id = id + 10, age = age * 2 WHERE age >= 30").rowcount == 2
    assert rows(db, "SELECT id, age FROM users") == [(1, 20), (12, 60), (13, 80)]
    age_index = db.catalog.get_table("users").index_on([3])
    assert age_index is not None and len(age_index.tree.search((60,))) == 1
    assert age_index.tree.search((30,)) == []
    assert consistent(db, "users") == 3


def test_update_that_moves_rows_updates_each_row_once(db: Database) -> None:
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, pad TEXT)")
    db.execute("INSERT INTO t VALUES " + ", ".join(f"({i}, 'x')" for i in range(300)))
    # every row grows to ~1 KB, so most rows move to new pages during the update
    result = db.execute("UPDATE t SET pad = pad || '" + "y" * 1000 + "'")
    assert result.rowcount == 300
    assert {len(str(r[0])) for r in rows(db, "SELECT pad FROM t")} == {1001}
    assert consistent(db, "t") == 300


def test_delete(db: Database) -> None:
    db.execute(USERS)
    db.execute("INSERT INTO users (id, name, age) VALUES (1, 'a', 10), (2, 'b', 20), (3, 'c', 30)")
    assert db.execute("DELETE FROM users WHERE age < 25").message == "DELETE 2"
    assert rows(db, "SELECT id FROM users") == [(3,)]
    db.execute("INSERT INTO users (id, name) VALUES (1, 'again')")  # the key is free again
    assert db.execute("DELETE FROM users").rowcount == 2
    assert consistent(db, "users") == 0


def test_ddl_statements(db: Database) -> None:
    db.execute(USERS)
    assert db.execute("CREATE TABLE IF NOT EXISTS users (x INT)").message.endswith(
        "(already exists)"
    )
    with pytest.raises(DuplicateObjectError):
        db.execute("CREATE TABLE users (x INT)")
    with pytest.raises(CatalogError, match="multiple primary keys"):
        db.execute("CREATE TABLE bad (a INT PRIMARY KEY, b INT PRIMARY KEY)")
    with pytest.raises(TypeMismatchError, match="invalid DEFAULT"):
        db.execute("CREATE TABLE bad (a INT DEFAULT 'x')")
    db.execute("CREATE TABLE pair (a INT, b TEXT, PRIMARY KEY (a, b))")
    pair = db.catalog.get_table("pair")
    assert [c.not_null for c in pair.columns] == [True, True]
    assert rows(db, "SHOW TABLES") == [("pair",), ("users",)]
    assert rows(db, "DESCRIBE users") == [
        ("id", "INT", False, None, "PRI"),
        ("name", "VARCHAR(8)", False, None, None),
        ("email", "TEXT", True, None, "UNI"),
        ("age", "INT", True, "18", None),
        ("active", "BOOL", True, "TRUE", None),
    ]
    db.execute("CREATE INDEX users_age ON users (age)")
    db.execute("DROP INDEX users_age")
    assert db.execute("DROP INDEX IF EXISTS users_age").message.endswith("(did not exist)")
    db.execute("DROP TABLE pair")
    assert db.execute("DROP TABLE IF EXISTS pair").message.endswith("(did not exist)")
    assert rows(db, "SHOW TABLES") == [("users",)]


@pytest.mark.parametrize(
    "sql",
    ["BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN SELECT 1", "ANALYZE t"],
)
def test_unsupported_statements_say_so(db: Database, sql: str) -> None:
    with pytest.raises(NotSupportedError):
        db.execute(sql)


def test_everything_persists(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    with Database(path, pool_size=8) as db:
        db.execute(USERS)
        db.execute("CREATE INDEX users_age ON users (age)")
        db.execute(
            "INSERT INTO users (id, name, age) VALUES "
            + ", ".join(f"({i}, 'u{i}', {i % 9})" for i in range(500))
        )
        db.execute("UPDATE users SET email = name || '@x' WHERE id < 100")
        db.execute("DELETE FROM users WHERE id >= 400")
    with Database(path, pool_size=8) as db:
        assert len(rows(db, "SELECT id FROM users")) == 400
        assert rows(db, "SELECT email FROM users WHERE id = 42") == [("u42@x",)]
        assert consistent(db, "users") == 400
        with pytest.raises(UniqueViolationError):
            db.execute("INSERT INTO users (id, name) VALUES (7, 'dup')")
