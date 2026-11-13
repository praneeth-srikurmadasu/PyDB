"""Index nested-loop join: look each outer row's key up in an index of the inner table."""

from collections.abc import Iterator, Sequence

from pydb.catalog.schema import IndexInfo, TableInfo
from pydb.execution.expressions import Evaluator
from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row


class IndexNestedLoopJoin(Operator):
    """For each outer row, evaluates ``outer_keys`` and searches ``index`` (whose leading
    columns they match) for inner rows: O(outer x log inner) instead of a full inner scan.

    ``residual`` is the rest of the ON condition. Keys with NULL find nothing. For a LEFT
    join, unmatched outer rows are padded with NULLs.
    """

    def __init__(
        self,
        outer: Operator,
        table: TableInfo,
        index: IndexInfo,
        outer_keys: Sequence[Evaluator],
        residual: Evaluator | None,
        kind: str,
        description: str = "",
    ) -> None:
        self.description = description
        self.outer = outer
        self.table = table
        self.index = index
        self.outer_keys = list(outer_keys)
        self.residual = residual
        self.kind = kind
        self.columns = outer.columns + tuple(table.column_names())
        self._padding = (None,) * len(table.columns)
        self._pending: Iterator[Row] = iter(())
        self.lookups = 0

    def label(self) -> str:
        return (
            f"IndexNestedLoopJoin {self.kind} {self.table.name} using {self.index.name} "
            f"{self.description}"
        ).rstrip()

    def children(self) -> list[Operator]:
        return [self.outer]

    def open(self) -> None:
        self.outer.open()
        self._pending = iter(())
        self.lookups = 0

    def next(self) -> Row | None:
        while True:
            row = next(self._pending, None)
            if row is not None:
                return row
            outer_row = self.outer.next()
            if outer_row is None:
                return None
            self._pending = iter(self._matches(outer_row))

    def _matches(self, outer_row: Row) -> list[Row]:
        key = tuple(k(outer_row) for k in self.outer_keys)
        rows: list[Row] = []
        if None not in key:
            self.lookups += 1
            for rid in self.index.tree.search(key):
                inner = self.table.layout.deserialize(self.table.heap.get(rid))
                rows.append(outer_row + inner)
        if self.residual is not None:
            rows = [r for r in rows if self.residual(r) is True]
        if not rows and self.kind == "LEFT":
            return [outer_row + self._padding]
        return rows

    def close(self) -> None:
        self.outer.close()
