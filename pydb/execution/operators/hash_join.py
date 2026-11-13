"""Hash join on equality keys."""

from collections.abc import Iterator, Sequence

from pydb.execution.expressions import Evaluator
from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row
from pydb.types import Value


class HashJoin(Operator):
    """Builds a hash table of the inner input keyed by its join keys, then probes it with
    each outer row: O(outer + inner) with the inner input held in memory.

    Keys containing NULL never match, as ``NULL = x`` is never TRUE. ``residual`` is the rest
    of the ON condition, checked on each candidate pair. For a LEFT join, unmatched outer
    rows are padded with NULLs. Output rows are always outer columns, then inner columns.
    """

    def __init__(
        self,
        outer: Operator,
        inner: Operator,
        outer_keys: Sequence[Evaluator],
        inner_keys: Sequence[Evaluator],
        residual: Evaluator | None,
        kind: str,
    ) -> None:
        self.outer = outer
        self.inner = inner
        self.outer_keys = list(outer_keys)
        self.inner_keys = list(inner_keys)
        self.residual = residual
        self.kind = kind
        self.columns = outer.columns + inner.columns
        self._padding = (None,) * len(inner.columns)
        self._table: dict[tuple[Value, ...], list[Row]] = {}
        self._pending: Iterator[Row] = iter(())
        self.build_rows = 0

    def open(self) -> None:
        self._table = {}
        self.build_rows = 0
        for row in self.inner.rows():
            self.build_rows += 1
            key = tuple(k(row) for k in self.inner_keys)
            if None not in key:
                self._table.setdefault(key, []).append(row)
        self.outer.open()
        self._pending = iter(())

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
        found = [] if None in key else self._table.get(key, [])
        rows = [outer_row + inner_row for inner_row in found]
        if self.residual is not None:
            rows = [r for r in rows if self.residual(r) is True]
        if not rows and self.kind == "LEFT":
            return [outer_row + self._padding]
        return rows

    def close(self) -> None:
        self.outer.close()
        self._table = {}
