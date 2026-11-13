import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.catalog.catalog import Catalog
from pydb.errors import (
    AmbiguousColumnError,
    CatalogError,
    NotSupportedError,
    TypeMismatchError,
    UnknownColumnError,
    UnknownFunctionError,
    UnknownTableError,
)
from pydb.execution.executor import Executor
from pydb.execution.expressions import BoundColumn
from pydb.planner.binder import Binder, Scope, ScopeColumn
from pydb.sql import ast
from pydb.sql.parser import parse, parse_expression
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.types import DataType


@pytest.fixture
def binder(tmp_path: Path) -> Iterator[Binder]:
    disk = DiskManager(tmp_path / "bind.db")
    catalog = Catalog(BufferPoolManager(disk, 32))
    Executor(catalog).execute(
        parse("CREATE TABLE users (id INT PRIMARY KEY, name TEXT, score FLOAT, ok BOOL)")
    )
    yield Binder(catalog)
    disk.close()


def parsed[S](sql: str, kind: type[S]) -> S:
    node = parse(sql)
    assert isinstance(node, kind)
    return node


def select(binder: Binder, sql: str) -> object:
    return binder.bind_select(parsed(sql, ast.Select))


def test_star_expands_to_all_columns(binder: Binder) -> None:
    bound = binder.bind_select(parsed("SELECT * FROM users", ast.Select))
    assert [name for name, _ in bound.outputs] == ["id", "name", "score", "ok"]
    assert [e.index for _, e in bound.outputs if isinstance(e, BoundColumn)] == [0, 1, 2, 3]


def test_output_names(binder: Binder) -> None:
    bound = binder.bind_select(
        parsed("SELECT id, score * 2, LOWER(name) AS n, u.ok FROM users u", ast.Select)
    )
    assert [name for name, _ in bound.outputs] == ["id", "(score * 2)", "n", "ok"]


def test_result_types(binder: Binder) -> None:
    scope = Scope.for_table(binder.catalog.get_table("users"))
    types = {
        "id + 1": DataType.INT,
        "id + score": DataType.FLOAT,
        "id / 2": DataType.INT,
        "name || 'x'": DataType.TEXT,
        "id > 1 AND ok": DataType.BOOL,
        "LENGTH(name)": DataType.INT,
        "ABS(score)": DataType.FLOAT,
        "COALESCE(id, score)": DataType.FLOAT,
        "NULL": None,
    }
    for sql, expected in types.items():
        assert binder.bind_expr(parse_expression(sql), scope).type == expected, sql


@pytest.mark.parametrize(
    ("sql", "error", "message"),
    [
        ("SELECT nope FROM users", UnknownColumnError, "available columns: users.id"),
        ("SELECT x.id FROM users", UnknownColumnError, 'missing table "x"'),
        ("SELECT users.id FROM users u", UnknownColumnError, 'missing table "users"'),
        ("SELECT * FROM missing", UnknownTableError, 'table "missing" does not exist'),
        ("SELECT id FROM users WHERE name", TypeMismatchError, "WHERE expects BOOL, got TEXT"),
        ("SELECT id FROM users WHERE id = 'x'", TypeMismatchError, "cannot compare INT with TEXT"),
        ("SELECT name + 1 FROM users", TypeMismatchError, "+ expects FLOAT or INT, got TEXT"),
        ("SELECT id || 'x' FROM users", TypeMismatchError, "|| expects TEXT, got INT"),
        ("SELECT NOT id FROM users", TypeMismatchError, "NOT expects BOOL"),
        ("SELECT id FROM users WHERE id LIKE 'x'", TypeMismatchError, "LIKE expects TEXT"),
        ("SELECT FOO(id) FROM users", UnknownFunctionError, "function FOO does not exist"),
        ("SELECT LOWER(name, name) FROM users", TypeMismatchError, "exactly one argument"),
        ("SELECT *", UnknownColumnError, "needs a FROM clause"),
        ("SELECT COUNT(*) FROM users", NotSupportedError, "aggregate function COUNT"),
        ("SELECT id FROM users ORDER BY id", NotSupportedError, "ORDER BY"),
        ("SELECT id FROM users u JOIN users v ON TRUE", NotSupportedError, "JOIN"),
    ],
)
def test_select_errors(binder: Binder, sql: str, error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=re.escape(message)):
        select(binder, sql)


def test_ambiguous_column() -> None:
    scope = Scope([ScopeColumn("a", "id", DataType.INT), ScopeColumn("b", "id", DataType.INT)])
    binder = Binder.__new__(Binder)
    with pytest.raises(AmbiguousColumnError, match='"id" is ambiguous'):
        binder.bind_expr(ast.ColumnRef("id"), scope)
    bound = binder.bind_expr(ast.ColumnRef("id", "b"), scope)
    assert isinstance(bound, BoundColumn) and bound.index == 1


def test_insert_binding(binder: Binder) -> None:
    bound = binder.bind_insert(parsed("INSERT INTO users (name, id) VALUES ('a', 1)", ast.Insert))
    assert bound.positions == (1, 0)
    with pytest.raises(TypeMismatchError, match="2 values but 1 target"):
        binder.bind_insert(parsed("INSERT INTO users (id) VALUES (1, 2)", ast.Insert))
    with pytest.raises(TypeMismatchError, match='column "id" is INT but the value is FLOAT'):
        binder.bind_insert(parsed("INSERT INTO users (id) VALUES (1.5)", ast.Insert))
    with pytest.raises(UnknownColumnError, match='column "x" of table "users"'):
        binder.bind_insert(parsed("INSERT INTO users (x) VALUES (1)", ast.Insert))
    with pytest.raises(CatalogError, match="more than once"):
        binder.bind_insert(parsed("INSERT INTO users (id, id) VALUES (1, 2)", ast.Insert))
    with pytest.raises(UnknownColumnError):
        binder.bind_insert(parsed("INSERT INTO users (id) VALUES (id)", ast.Insert))
    binder.bind_insert(parsed("INSERT INTO users (score) VALUES (1)", ast.Insert))  # INT -> FLOAT


def test_update_and_delete_binding(binder: Binder) -> None:
    bound = binder.bind_update(parsed("UPDATE users SET score = score + id WHERE ok", ast.Update))
    assert bound.assignments[0][0] == 2
    with pytest.raises(CatalogError, match="assigned more than once"):
        binder.bind_update(parsed("UPDATE users SET id = 1, id = 2", ast.Update))
    with pytest.raises(TypeMismatchError):
        binder.bind_update(parsed("UPDATE users SET ok = 1", ast.Update))
    with pytest.raises(TypeMismatchError, match="WHERE expects BOOL"):
        binder.bind_delete(parsed("DELETE FROM users WHERE id", ast.Delete))


def test_system_tables_are_read_only(binder: Binder) -> None:
    select(binder, "SELECT name FROM __tables__")
    with pytest.raises(CatalogError, match="read-only"):
        binder.bind_delete(parsed("DELETE FROM __tables__", ast.Delete))
