"""Operators exercised directly, without SQL."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from pydb.catalog.catalog import Catalog
from pydb.catalog.schema import Column, TableInfo
from pydb.execution.operators.aggregate import AggregateSpec, HashAggregate
from pydb.execution.operators.base import Operator
from pydb.execution.operators.distinct import Distinct
from pydb.execution.operators.hash_join import HashJoin
from pydb.execution.operators.index_nl_join import IndexNestedLoopJoin
from pydb.execution.operators.limit import Limit
from pydb.execution.operators.nested_loop_join import NestedLoopJoin
from pydb.execution.operators.seq_scan import SeqScan
from pydb.execution.operators.sort import Sort, TopN, compare_rows
from pydb.execution.operators.values import Values
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.disk_manager import DiskManager
from pydb.storage.tuple import Row
from pydb.types import DataType, Value

INT, TEXT = DataType.INT, DataType.TEXT


class Counting(Values):
    """Values that records how many rows were pulled from it."""

    def __init__(self, rows: list[Row]) -> None:
        super().__init__(rows, ("x",))
        self.pulled = 0

    def next(self) -> Row | None:
        row = super().next()
        if row is not None:
            self.pulled += 1
        return row


def col(i: int):  # type: ignore[no-untyped-def]
    return lambda row: row[i]


def vals(rows: list[Row], width: int = 2) -> Values:
    return Values(rows, tuple(f"c{i}" for i in range(width)))


def run(op: Operator) -> list[Row]:
    return list(op.rows())


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[BufferPoolManager]:
    disk = DiskManager(tmp_path / "ops.db")
    bpm = BufferPoolManager(disk, 8)
    yield bpm
    assert bpm.pinned_pages() == {}
    disk.close()


@pytest.fixture
def people(pool: BufferPoolManager) -> TableInfo:
    catalog = Catalog(pool)
    table = catalog.create_table("p", [Column("id", INT), Column("name", TEXT)], primary_key=[0])
    for rid_row in [(1, "a"), (2, "b"), (3, "c")]:
        rid = table.heap.insert(table.layout.serialize(rid_row))
        table.indexes[0].tree.insert((rid_row[0],), rid)
    return table


LEFT: list[Row] = [(1, "x"), (2, "y"), (None, "z"), (4, "w")]
RIGHT: list[Row] = [(1, "one"), (1, "uno"), (2, "two"), (None, "null")]
INNER_EXPECTED = [(1, "x", 1, "one"), (1, "x", 1, "uno"), (2, "y", 2, "two")]
LEFT_EXPECTED = [*INNER_EXPECTED, (None, "z", None, None), (4, "w", None, None)]


def test_nested_loop_join() -> None:
    def eq(row: Row) -> Value:
        return None if row[0] is None or row[2] is None else row[0] == row[2]

    inner = NestedLoopJoin(vals(LEFT), vals(RIGHT), eq, "INNER")
    assert run(inner) == INNER_EXPECTED
    assert sorted(run(NestedLoopJoin(vals(LEFT), vals(RIGHT), eq, "LEFT")), key=repr) == sorted(
        LEFT_EXPECTED, key=repr
    )
    cross = NestedLoopJoin(vals([(1,), (2,)], 1), vals([("a",), ("b",)], 1), None, "INNER")
    assert run(cross) == [(1, "a"), (1, "b"), (2, "a"), (2, "b")]


def test_hash_join_null_keys_never_match() -> None:
    inner = HashJoin(vals(LEFT), vals(RIGHT), [col(0)], [col(0)], None, "INNER")
    assert run(inner) == INNER_EXPECTED
    left = HashJoin(vals(LEFT), vals(RIGHT), [col(0)], [col(0)], None, "LEFT")
    assert run(left) == [
        (1, "x", 1, "one"),
        (1, "x", 1, "uno"),
        (2, "y", 2, "two"),
        (None, "z", None, None),
        (4, "w", None, None),
    ]


def test_hash_join_residual_applies_before_left_padding() -> None:
    def not_uno(row: Row) -> Value:
        return row[3] != "uno"

    join = HashJoin(vals([(1, "x")]), vals(RIGHT), [col(0)], [col(0)], not_uno, "LEFT")
    assert run(join) == [(1, "x", 1, "one")]

    def never(row: Row) -> Value:
        return False

    padded = HashJoin(vals([(1, "x")]), vals(RIGHT), [col(0)], [col(0)], never, "LEFT")
    assert run(padded) == [(1, "x", None, None)]


def test_index_nested_loop_join(people: TableInfo) -> None:
    outer = Values([(3,), (9,), (None,), (1,)], ("k",))
    join = IndexNestedLoopJoin(outer, people, people.indexes[0], [col(0)], None, "LEFT")
    assert run(join) == [(3, 3, "c"), (9, None, None), (None, None, None), (1, 1, "a")]
    assert join.lookups == 3  # the NULL key is never looked up


def test_joins_over_real_scans(people: TableInfo) -> None:
    scan_join = HashJoin(SeqScan(people), SeqScan(people), [col(0)], [col(0)], None, "INNER")
    assert run(scan_join) == [(1, "a", 1, "a"), (2, "b", 2, "b"), (3, "c", 3, "c")]

    def less(row: Row) -> Value:
        a, b = row[0], row[2]
        return isinstance(a, int) and isinstance(b, int) and a < b

    loop = NestedLoopJoin(SeqScan(people), SeqScan(people), less, "INNER")
    assert len(run(loop)) == 3


def test_limit_stops_pulling() -> None:
    source = Counting([(i,) for i in range(100)])
    assert run(Limit(source, 3, offset=5)) == [(5,), (6,), (7,)]
    assert source.pulled == 8
    assert run(Limit(Values([(1,), (2,)]), None, offset=1)) == [(2,)]
    assert run(Limit(Values([(1,)]), 5, offset=10)) == []


def test_distinct_treats_nulls_as_equal() -> None:
    rows: list[Row] = [(1, None), (1, None), (2, "a"), (1, None), (2, "a"), (2, "b")]
    assert run(Distinct(Values(rows))) == [(1, None), (2, "a"), (2, "b")]


def test_hash_aggregate() -> None:
    rows: list[Row] = [("a", 1), ("b", None), ("a", 3), (None, 5), ("b", 2)]
    specs = [
        AggregateSpec("COUNT", None),
        AggregateSpec("COUNT", col(1)),
        AggregateSpec("SUM", col(1)),
        AggregateSpec("AVG", col(1)),
        AggregateSpec("MIN", col(1)),
        AggregateSpec("MAX", col(1)),
    ]
    agg = HashAggregate(Values(rows), [col(0)], specs, ["g", "c", "cn", "s", "a", "mn", "mx"])
    assert run(agg) == [
        ("a", 2, 2, 4, 2.0, 1, 3),
        ("b", 2, 1, 2, 2.0, 2, 2),
        (None, 1, 1, 5, 5.0, 5, 5),
    ]


def test_aggregate_of_empty_input() -> None:
    specs = [AggregateSpec("COUNT", None), AggregateSpec("SUM", col(0))]
    assert run(HashAggregate(Values([]), [], specs, ["c", "s"])) == [(0, None)]
    assert run(HashAggregate(Values([]), [col(0)], specs, ["g", "c", "s"])) == []


def test_sort_order_with_nulls() -> None:
    keys = [(0, False)]
    assert compare_rows(keys, (None,), (1,)) == -1  # NULL first ascending
    assert compare_rows([(0, True)], (None,), (1,)) == 1  # and last descending
    assert compare_rows(keys, (None,), (None,)) == 0


def test_in_memory_sort_is_stable(pool: BufferPoolManager) -> None:
    rows: list[Row] = [(2, "a"), (1, "b"), (2, "c"), (None, "d"), (1, "e")]
    op = Sort(Values(rows, ("k", "v")), [(0, False)], [INT, TEXT], pool, 1 << 20)
    assert run(op) == [(None, "d"), (1, "b"), (1, "e"), (2, "a"), (2, "c")]
    assert op.runs_written == 0
    desc = Sort(Values(rows), [(0, True), (1, True)], [INT, TEXT], pool, 1 << 20)
    assert run(desc) == [(2, "c"), (2, "a"), (1, "e"), (1, "b"), (None, "d")]


def test_external_sort_spills_merges_and_frees_runs(pool: BufferPoolManager) -> None:
    rows: list[Row] = [((i * 7919) % 1000, "x" * 100) for i in range(1000)]
    free_before = pool.disk.free_page_count
    op = Sort(Values(rows), [(0, False)], [INT, TEXT], pool, memory_bytes=4096)
    out = run(op)
    assert [r[0] for r in out] == sorted(int(str(r[0])) for r in rows)
    assert op.runs_written > 10
    assert pool.disk.free_page_count > free_before  # run pages were returned


def test_top_n() -> None:
    rows: list[Row] = [(i % 10, i) for i in range(50)]
    top = run(TopN(Values(rows), [(0, True), (1, False)], 4))
    assert top == [(9, 9), (9, 19), (9, 29), (9, 39)]
    assert run(TopN(Values([(1,)]), [(0, False)], 0)) == []
