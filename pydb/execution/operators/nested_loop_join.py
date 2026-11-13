"""Nested-loop join: for every outer row, scan the whole inner input."""

from pydb.execution.expressions import Evaluator
from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row


class NestedLoopJoin(Operator):
    """Joins with any ON condition. Costs one inner scan per outer row: O(outer x inner).

    For a LEFT join, an outer row with no matching inner row is emitted once, padded with
    NULLs for the inner columns.
    """

    def __init__(
        self,
        outer: Operator,
        inner: Operator,
        predicate: Evaluator | None,
        kind: str,
        description: str = "",
    ) -> None:
        self.description = description
        self.outer = outer
        self.inner = inner
        self.predicate = predicate
        self.kind = kind
        self.columns = outer.columns + inner.columns
        self._padding = (None,) * len(inner.columns)
        self._outer_row: Row | None = None
        self._matched = False

    def label(self) -> str:
        return f"NestedLoopJoin {self.kind} {self.description}".rstrip()

    def children(self) -> list[Operator]:
        return [self.outer, self.inner]

    def open(self) -> None:
        self.outer.open()
        self._outer_row = None

    def next(self) -> Row | None:
        while True:
            if self._outer_row is None:
                self._outer_row = self.outer.next()
                if self._outer_row is None:
                    return None
                self._matched = False
                self.inner.close()
                self.inner.open()
            inner_row = self.inner.next()
            if inner_row is None:
                outer_row, self._outer_row = self._outer_row, None
                if self.kind == "LEFT" and not self._matched:
                    return outer_row + self._padding
                continue
            row = self._outer_row + inner_row
            if self.predicate is None or self.predicate(row) is True:
                self._matched = True
                return row

    def close(self) -> None:
        self.outer.close()
        self.inner.close()
