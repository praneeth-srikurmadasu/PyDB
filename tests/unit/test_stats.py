from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.catalog.catalog import STATS_TABLE
from pydb.database import Database
from pydb.execution.expressions import (
    BoundBetween,
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundInList,
    BoundIsNull,
    BoundLiteral,
    BoundUnary,
)
from pydb.planner.stats import ColumnStats, collect, selectivity, table_size
from pydb.types import DataType

INT = DataType.INT


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    with Database(tmp_path / "stats.db") as database:
        database.execute("CREATE TABLE t (a INT, b TEXT, c FLOAT)")
        database.execute(
            "INSERT INTO t VALUES (1, 'x', 0.5), (2, 'y', NULL), (2, NULL, 1.5), (5, 'x', NULL)"
        )
        yield database


def test_collect(db: Database) -> None:
    stats = collect(db.catalog.get_table("t"))
    assert stats.row_count == 4
    assert stats.page_count == 1
    assert stats.columns == (
        ColumnStats(distinct=3, nulls=0, min=1, max=5),
        ColumnStats(distinct=2, nulls=1, min="x", max="y"),
        ColumnStats(distinct=2, nulls=2, min=0.5, max=1.5),
    )


def test_analyze_persists_and_drop_removes(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (a INT, b TEXT)")
        db.execute("INSERT INTO t VALUES (1, 'it''s'), (3, NULL)")
        assert db.execute("ANALYZE t").message == "ANALYZE"
        expected = db.catalog.get_table("t").stats
    with Database(path) as db:
        table = db.catalog.get_table("t")
        assert table.stats == expected
        assert table_size(table) == (2.0, 1.0)
        db.execute("DROP TABLE t")
        stats_table = db.catalog.get_table(STATS_TABLE)
        assert list(stats_table.heap.scan()) == []


def test_defaults_before_analyze(db: Database) -> None:
    assert table_size(db.catalog.get_table("t")) == (1000.0, 20.0)


def test_database_without_stats_table_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t (a INT)")
        # Make the file look like one written before __stats__ existed.
        catalog = db.catalog
        stats_id = catalog.get_table(STATS_TABLE).table_id
        catalog._delete_system_rows("__columns__", lambda r: r[0] == stats_id)
        catalog._delete_system_rows("__tables__", lambda r: r[0] == stats_id)
    with Database(path) as db:
        assert db.catalog.has_table(STATS_TABLE)
        db.execute("ANALYZE t")
    with Database(path) as db:
        assert db.catalog.get_table("t").stats is not None


# --------------------------------------------------------------------- selectivity

A = BoundColumn(0, INT, "a")
B = BoundColumn(1, INT, "b")


def lit(v: object) -> BoundLiteral:
    return BoundLiteral(v, INT)  # type: ignore[arg-type]


def cmp(op: str, left: BoundExpr, right: BoundExpr) -> BoundBinary:
    return BoundBinary(op, left, right, DataType.BOOL)


A_STATS = ColumnStats(distinct=20, nulls=10, min=0, max=100)
B_STATS = ColumnStats(distinct=50, nulls=0, min=0, max=10)


def with_stats(index: int) -> tuple[ColumnStats | None, float]:
    return ([A_STATS, B_STATS][index], 100.0)


def without_stats(index: int) -> tuple[ColumnStats | None, float]:
    return None, 1000.0


@pytest.mark.parametrize(
    ("expr", "known", "unknown"),
    [
        (cmp("=", A, lit(5)), 1 / 20, 0.1),
        (cmp("=", lit(5), A), 1 / 20, 0.1),
        (cmp("!=", A, lit(5)), 1 - 1 / 20, 0.9),
        (cmp("<", A, lit(25)), 0.25, 1 / 3),
        (cmp(">", A, lit(25)), 0.75, 1 / 3),
        (cmp(">", lit(25), A), 0.25, 1 / 3),  # 25 > a means a < 25
        (cmp("<", A, lit(500)), 1.0, 1 / 3),  # clamped
        (BoundBetween(A, lit(10), lit(30), False), 0.2, 1 / 3),
        (BoundBetween(A, lit(10), lit(30), True), 0.8, 2 / 3),
        (BoundInList(A, (lit(1), lit(2), lit(2)), False), 2 / 20, 0.2),
        (BoundIsNull(A, False), 0.1, 1 / 3),
        (cmp("=", A, B), 1 / 50, 0.1),
        (cmp("AND", cmp("=", A, lit(1)), cmp("<", A, lit(50))), 1 / 20 * 0.5, 0.1 / 3),
        (cmp("OR", cmp("=", A, lit(1)), cmp("=", B, lit(1))), 0.05 + 0.02 - 0.001, 0.19),
        (BoundUnary("NOT", cmp("=", A, lit(1)), DataType.BOOL), 0.95, 0.9),
        (BoundLiteral(True, DataType.BOOL), 1.0, 1.0),
        (BoundLiteral(False, DataType.BOOL), 0.0, 0.0),
    ],
)
def test_selectivity_rules(expr: BoundExpr, known: float, unknown: float) -> None:
    assert selectivity(expr, with_stats) == pytest.approx(known)
    assert selectivity(expr, without_stats) == pytest.approx(unknown)
