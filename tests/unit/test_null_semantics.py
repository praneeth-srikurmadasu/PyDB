"""SQL three-valued logic: NULL is "unknown", and WHERE keeps only TRUE."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.database import Database
from pydb.execution.expressions import sql_and, sql_not, sql_or
from pydb.types import Value

T, F, N = True, False, None


@pytest.mark.parametrize(
    ("a", "b", "and_", "or_"),
    [
        (T, T, T, T),
        (T, F, F, T),
        (T, N, N, T),
        (F, F, F, F),
        (F, N, F, N),  # NULL AND FALSE is FALSE
        (N, N, N, N),
    ],
)
def test_truth_tables(a: Value, b: Value, and_: Value, or_: Value) -> None:
    assert sql_and(a, b) is and_ and sql_and(b, a) is and_
    assert sql_or(a, b) is or_ and sql_or(b, a) is or_


def test_not() -> None:
    assert sql_not(T) is F
    assert sql_not(F) is T
    assert sql_not(N) is N


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    with Database(tmp_path / "nulls.db") as database:
        database.execute_script(
            "CREATE TABLE t (id INT, v INT, s TEXT);"
            "INSERT INTO t VALUES (1, 10, 'a'), (2, NULL, NULL), (3, 30, 'c');"
        )
        yield database


def value(db: Database, expr: str) -> Value:
    return db.execute(f"SELECT {expr}").rows[0][0]


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("NULL = NULL", None),
        ("NULL != 1", None),
        ("NULL < 1", None),
        ("NULL AND FALSE", False),
        ("NULL AND TRUE", None),
        ("NULL OR TRUE", True),
        ("NULL OR FALSE", None),
        ("NOT NULL", None),
        ("NULL IS NULL", True),
        ("NULL IS NOT NULL", False),
        ("1 + NULL", None),
        ("'a' || NULL", None),
        ("1 IN (1, NULL)", True),
        ("2 IN (1, NULL)", None),
        ("2 NOT IN (1, NULL)", None),
        ("2 NOT IN (1, 3)", True),
        ("NULL IN (1)", None),
        ("NULL BETWEEN 1 AND 2", None),
        ("5 BETWEEN NULL AND 2", False),  # 5 <= 2 is FALSE, so the AND is FALSE
        ("5 BETWEEN 1 AND NULL", None),
        ("NULL LIKE 'a'", None),
        ("COALESCE(NULL, NULL, 3)", 3),
        ("COALESCE(NULL, NULL)", None),
        ("LENGTH(NULL)", None),
        ("UPPER(NULL)", None),
    ],
)
def test_expression_results(db: Database, expr: str, expected: Value) -> None:
    assert value(db, expr) == expected
    assert type(value(db, expr)) is type(expected)


def ids(db: Database, where: str) -> list[Value]:
    return [row[0] for row in db.execute(f"SELECT id FROM t WHERE {where}").rows]


def test_where_keeps_only_true(db: Database) -> None:
    assert ids(db, "v > 15") == [3]
    assert ids(db, "NOT (v > 15)") == [1]  # row 2: NOT NULL is NULL, not TRUE
    assert ids(db, "v = NULL") == []
    assert ids(db, "v != 10") == [3]
    assert ids(db, "v IS NULL") == [2]
    assert ids(db, "v IS NOT NULL") == [1, 3]
    assert ids(db, "v > 15 OR s IS NULL") == [2, 3]
    assert ids(db, "s LIKE '%'") == [1, 3]


def test_update_and_delete_with_null_predicates(db: Database) -> None:
    assert db.execute("UPDATE t SET v = 0 WHERE v < 100").rowcount == 2
    assert db.execute("DELETE FROM t WHERE v != 0").rowcount == 0
    assert db.execute("DELETE FROM t WHERE v IS NULL").rowcount == 1
