"""ORDER BY: an in-memory sort that becomes an external merge sort when rows do not fit.

Rows are buffered until their serialized size exceeds ``memory_bytes``. Each full buffer is
sorted and written as a *run* to temporary pages (a table heap in the database file, through
the buffer pool). At the end the runs, plus whatever remains in memory, are merged with a
k-way merge that reads each run sequentially. The temporary pages are freed on ``close()``.

Order: NULLs sort before every value in ascending order and after every value in descending
order (as in SQLite, and as in PyDB's index keys). The sort is stable.
"""

import functools
import heapq
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from pydb.execution.operators.base import Operator
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.storage.table_heap import TableHeap
from pydb.storage.tuple import Row, RowLayout
from pydb.types import DataType

SortKey = tuple[int, bool]  # (column position, descending)


def compare_rows(keys: Sequence[SortKey], a: Row, b: Row) -> int:
    for position, descending in keys:
        x, y = a[position], b[position]
        if x == y:  # includes both NULL
            continue
        if x is None:
            result = -1
        elif y is None:
            result = 1
        else:
            result = -1 if x < y else 1  # type: ignore[operator]
        return -result if descending else result
    return 0


def sort_key(keys: Sequence[SortKey]) -> Callable[[Row], Any]:
    return functools.cmp_to_key(functools.partial(compare_rows, list(keys)))


class Sort(Operator):
    def __init__(
        self,
        child: Operator,
        keys: Sequence[SortKey],
        types: Sequence[DataType | None],
        pool: BufferPoolManager,
        memory_bytes: int,
    ) -> None:
        self.child = child
        self.columns = child.columns
        self.keys = list(keys)
        self.pool = pool
        self.memory_bytes = memory_bytes
        # a column whose type is unknown only ever holds NULL, which every type can store
        self.layout = RowLayout([t or DataType.INT for t in types])
        self._runs: list[TableHeap] = []
        self._output: Iterator[Row] = iter(())
        self.runs_written = 0
        self.rows_sorted = 0

    def open(self) -> None:
        self.close()
        key = sort_key(self.keys)
        buffer: list[Row] = []
        buffered_bytes = 0
        self.rows_sorted = 0
        for row in self.child.rows():
            self.rows_sorted += 1
            buffer.append(row)
            buffered_bytes += len(self.layout.serialize(row))
            if buffered_bytes > self.memory_bytes:
                self._spill(sorted(buffer, key=key))
                buffer, buffered_bytes = [], 0
        buffer.sort(key=key)
        if not self._runs:
            self._output = iter(buffer)
            return
        streams = [self._read_run(run) for run in self._runs] + [iter(buffer)]
        self._output = heapq.merge(*streams, key=key)

    def next(self) -> Row | None:
        return next(self._output, None)

    def close(self) -> None:
        self._output = iter(())
        for run in self._runs:
            run.drop()
        self._runs = []

    def _spill(self, rows: list[Row]) -> None:
        run = TableHeap.create(self.pool)
        for row in rows:
            run.insert(self.layout.serialize(row))
        self._runs.append(run)
        self.runs_written += 1

    def _read_run(self, run: TableHeap) -> Iterator[Row]:
        for _, data in run.scan():
            yield self.layout.deserialize(data)


class TopN(Operator):
    """ORDER BY ... LIMIT n: keeps only the ``count`` smallest rows, in O(count) memory."""

    def __init__(self, child: Operator, keys: Sequence[SortKey], count: int) -> None:
        self.child = child
        self.columns = child.columns
        self.keys = list(keys)
        self.count = count
        self._output: Iterator[Row] = iter(())

    def open(self) -> None:
        best = heapq.nsmallest(self.count, self.child.rows(), key=sort_key(self.keys))
        self._output = iter(best)

    def next(self) -> Row | None:
        return next(self._output, None)

    def close(self) -> None:
        self._output = iter(())
