"""ORDER BY over more data than the buffer pool holds."""

from pathlib import Path

from pydb.config import PAGE_SIZE
from pydb.database import Database
from pydb.execution.operators.base import Operator
from pydb.execution.operators.sort import Sort
from pydb.planner.physical_plan import PlannerSettings
from pydb.sql.ast import Select
from pydb.sql.parser import parse


def find_sort(op: Operator) -> Sort:
    while not isinstance(op, Sort):
        op = op.child  # type: ignore[attr-defined]
    return op


def test_sort_larger_than_buffer_pool(tmp_path: Path) -> None:
    pool_frames = 8
    settings = PlannerSettings(sort_memory_bytes=16 * 1024)
    with Database(tmp_path / "big.db", pool_size=pool_frames, settings=settings) as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, k INT, pad TEXT)")
        rows = [(i, (i * 104729) % 5003, f"row-{i:05d}-" + "p" * 150) for i in range(4000)]
        for start in range(0, len(rows), 500):
            chunk = rows[start : start + 500]
            db.execute(
                "INSERT INTO t VALUES " + ", ".join(f"({a}, {b}, '{c}')" for a, b, c in chunk)
            )
        table_bytes = len(list(db.catalog.get_table("t").heap.page_ids())) * PAGE_SIZE
        assert table_bytes > 10 * pool_frames * PAGE_SIZE  # the data dwarfs the pool

        select = parse("SELECT k, id, pad FROM t ORDER BY k DESC, id")
        assert isinstance(select, Select)
        plan = db.executor.plan_select(select)
        result = list(plan.rows())
        expected = sorted(rows, key=lambda r: (-r[1], r[0]))
        assert [(r[0], r[1]) for r in result] == [(k, i) for i, k, _ in expected]
        assert result[0][2] == expected[0][2]
        assert find_sort(plan).runs_written > 20
        assert db.pool.pinned_pages() == {}
        assert db.pool.stats.evictions > 0
