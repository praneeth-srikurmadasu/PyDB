"""Optimizer decisions, checked through the operator trees it builds."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.database import Database
from pydb.execution.expressions import BoundBinary, BoundColumn, BoundLiteral
from pydb.execution.operators.base import Operator
from pydb.execution.operators.filter import Filter
from pydb.execution.operators.hash_join import HashJoin
from pydb.execution.operators.index_nl_join import IndexNestedLoopJoin
from pydb.execution.operators.index_scan import IndexScan
from pydb.execution.operators.nested_loop_join import NestedLoopJoin
from pydb.execution.operators.seq_scan import SeqScan
from pydb.planner.optimizer import fold_constants
from pydb.sql.ast import Select
from pydb.sql.parser import parse
from pydb.types import DataType

INT, BOOL = DataType.INT, DataType.BOOL


def lit(v: object, t: DataType | None = INT) -> BoundLiteral:
    return BoundLiteral(v, t)  # type: ignore[arg-type]


COL = BoundColumn(0, INT, "a")


def test_constant_folding() -> None:
    assert fold_constants(BoundBinary("*", lit(2), lit(5), INT)) == lit(10)
    nested = BoundBinary("=", COL, BoundBinary("+", lit(1), lit(2), INT), BOOL)
    assert fold_constants(nested) == BoundBinary("=", COL, lit(3), BOOL)
    predicate = BoundBinary("=", COL, lit(1), BOOL)
    assert fold_constants(BoundBinary("AND", predicate, lit(True, BOOL), BOOL)) == predicate
    assert fold_constants(BoundBinary("AND", lit(False, BOOL), predicate, BOOL)) == lit(False, BOOL)
    assert fold_constants(BoundBinary("OR", predicate, lit(True, BOOL), BOOL)) == lit(True, BOOL)
    assert fold_constants(BoundBinary("OR", lit(False, BOOL), predicate, BOOL)) == predicate
    division = BoundBinary("/", lit(1), lit(0), INT)
    assert fold_constants(division) == division  # left for runtime, not raised at plan time


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    with Database(tmp_path / "opt.db") as database:
        database.execute_script(
            "CREATE TABLE big (id INT PRIMARY KEY, grp INT, k INT, v TEXT);"
            "CREATE INDEX big_grp ON big (grp);"
            "CREATE INDEX big_k_grp ON big (k, grp);"
            "CREATE TABLE small (id INT PRIMARY KEY, label TEXT);"
        )
        database.execute(
            "INSERT INTO big VALUES "
            + ", ".join(f"({i}, {i % 4}, {i % 100}, 'v{i}')" for i in range(2000))
        )
        database.execute(
            "INSERT INTO small VALUES " + ", ".join(f"({i}, 'l{i}')" for i in range(20))
        )
        yield database


def plan(db: Database, sql: str) -> Operator:
    stmt = parse(sql)
    assert isinstance(stmt, Select)
    return db.executor.plan_select(stmt)


def find(op: Operator, kind: type[Operator]) -> list[Operator]:
    found = [op] if isinstance(op, kind) else []
    for child in op.children():
        found += find(child, kind)
    return found


def scan_of(db: Database, sql: str) -> Operator:
    scans = find(plan(db, sql), IndexScan) + find(plan(db, sql), SeqScan)
    assert len(scans) == 1
    return scans[0]


def test_index_selection_follows_selectivity(db: Database) -> None:
    point = scan_of(db, "SELECT v FROM big WHERE id = 7")
    assert isinstance(point, IndexScan) and point.index.name == "big_pkey"
    assert point.est_rows == pytest.approx(1 / 1000 * 1000)  # unique key: one row
    assert isinstance(scan_of(db, "SELECT v FROM big WHERE id > 10"), SeqScan)  # default 1/3
    db.execute("ANALYZE big")
    narrow = scan_of(db, "SELECT v FROM big WHERE id > 1990")
    assert isinstance(narrow, IndexScan)
    assert narrow.label().endswith("(> 1990)")
    assert isinstance(scan_of(db, "SELECT v FROM big WHERE id > 1000"), SeqScan)
    # grp has 4 distinct values: an equality keeps 25%, so the index is not worth it
    assert isinstance(scan_of(db, "SELECT v FROM big WHERE grp = 2"), SeqScan)


def test_index_choice_without_statistics_uses_defaults(db: Database) -> None:
    grp = scan_of(db, "SELECT v FROM big WHERE grp = 2")  # 1/10 < 15%
    assert isinstance(grp, IndexScan) and grp.index.name == "big_grp"


def test_composite_index_equality_prefix_and_range(db: Database) -> None:
    db.execute("ANALYZE big")
    scan = scan_of(db, "SELECT v FROM big WHERE k = 5 AND grp >= 1 AND grp < 3")
    assert isinstance(scan, IndexScan) and scan.index.name == "big_k_grp"
    assert "(5, 1)" in scan.label() and "(5, 3)" in scan.label()
    # every row with k = 5 has grp = 1 (k = i % 100 and grp = i % 4, and 4 divides 100)
    assert db.execute("SELECT COUNT(*) FROM big WHERE k = 5 AND grp >= 1 AND grp < 3").rows == [
        (20,)
    ]
    assert db.execute("SELECT COUNT(*) FROM big WHERE k = 5 AND grp > 1 AND grp < 4").rows == [(0,)]


def test_in_list_and_between_use_index(db: Database) -> None:
    db.execute("ANALYZE big")
    in_scan = scan_of(db, "SELECT v FROM big WHERE id IN (5, 3, 5, 9)")
    assert isinstance(in_scan, IndexScan) and len(in_scan.ranges) == 3
    between = scan_of(db, "SELECT v FROM big WHERE id BETWEEN 100 AND 110")
    assert isinstance(between, IndexScan)
    assert db.execute("SELECT id FROM big WHERE id IN (5, 3, 5, 9)").rows == [(3,), (5,), (9,)]


def test_used_conditions_leave_the_filter(db: Database) -> None:
    tree = plan(db, "SELECT v FROM big WHERE id = 7 AND v != 'x'")
    (filter_op,) = find(tree, Filter)
    assert isinstance(filter_op, Filter) and "id" not in filter_op.description


def test_predicate_pushdown_below_join(db: Database) -> None:
    tree = plan(db, "SELECT b.v FROM big b JOIN small s ON b.grp = s.id WHERE s.label = 'l3'")
    (join,) = find(tree, HashJoin) + find(tree, NestedLoopJoin) + find(tree, IndexNestedLoopJoin)
    filters = find(join, Filter)
    assert any("label" in f.description for f in filters if isinstance(f, Filter))


def test_left_join_does_not_push_where_on_the_nullable_side(db: Database) -> None:
    sql = "SELECT b.id FROM small s LEFT JOIN big b ON b.grp = s.id WHERE b.id IS NULL"
    tree = plan(db, sql)
    top = tree
    while not isinstance(top, Filter):
        top = top.children()[0]
    assert "IS NULL" in top.label()  # applied above the join
    assert len(db.execute(sql).rows) == 16  # small ids 4..19 have no big rows


def test_join_order_starts_from_the_filtered_table(db: Database) -> None:
    db.execute("ANALYZE big")
    db.execute("ANALYZE small")
    tree = plan(db, "SELECT b.v FROM big b JOIN small s ON b.grp = s.id WHERE s.id = 2")
    joins = find(tree, HashJoin) + find(tree, NestedLoopJoin) + find(tree, IndexNestedLoopJoin)
    (join,) = joins
    outer = join.children()[0]
    assert "small" in outer.label() or any("small" in c.label() for c in outer.children())


def test_hash_join_builds_on_the_smaller_input(db: Database) -> None:
    db.execute("ANALYZE big")
    db.execute("ANALYZE small")
    tree = plan(db, "SELECT b.v FROM small s JOIN big b ON b.k = s.id + 1000")
    (join,) = find(tree, HashJoin)
    assert isinstance(join, HashJoin)
    # whichever side is outer, the hash table holds the 20-row table
    built = join.outer if join.build_outer else join.inner
    assert "small" in built.label()


def test_non_equi_join_uses_nested_loop(db: Database) -> None:
    tree = plan(db, "SELECT s.id FROM small s JOIN small t ON s.id < t.id")
    assert find(tree, NestedLoopJoin)


def test_index_nested_loop_for_a_small_outer_input(db: Database) -> None:
    db.execute("ANALYZE big")
    db.execute("ANALYZE small")
    tree = plan(db, "SELECT s.label FROM big b JOIN small s ON s.id = b.k WHERE b.id = 42")
    joins = find(tree, IndexNestedLoopJoin)
    assert joins and isinstance(joins[0], IndexNestedLoopJoin)
    assert joins[0].index.name == "small_pkey"
    assert (
        db.execute("SELECT s.label FROM big b JOIN small s ON s.id = b.k WHERE b.id = 42").rows
        == []
    )
    assert db.execute(
        "SELECT s.label FROM big b JOIN small s ON s.id = b.k WHERE b.id = 13"
    ).rows == [("l13",)]


def test_update_and_delete_use_index_access(db: Database) -> None:
    from pydb.sql.ast import Delete as DeleteStmt

    stmt = parse("DELETE FROM big WHERE id = 5")
    assert isinstance(stmt, DeleteStmt)
    bound = db.executor.binder.bind_delete(stmt)
    source = db.executor.optimizer.access_path(bound.table, bound.where)
    assert isinstance(source, IndexScan)
    assert db.execute("UPDATE big SET v = 'new' WHERE id = 5").rowcount == 1
    assert db.execute("DELETE FROM big WHERE id = 5").rowcount == 1
    assert db.execute("SELECT COUNT(*) FROM big").rows == [(1999,)]
