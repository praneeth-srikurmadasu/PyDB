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
        description: str = "",
        build_outer: bool = False,
    ) -> None:
        if build_outer and kind != "INNER":
            raise ValueError("only an INNER hash join can build on its outer input")
        self.description = description
        self.build_outer = build_outer
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
        self._probe: Operator = outer
        self.build_rows = 0

    def label(self) -> str:
        side = "outer" if self.build_outer else "inner"
        return f"HashJoin {self.kind} {self.description} (build {side})".replace("  ", " ")

    def children(self) -> list[Operator]:
        return [self.outer, self.inner]

    def open(self) -> None:
        self._table = {}
        self.build_rows = 0
        build, build_keys = (
            (self.outer, self.outer_keys) if self.build_outer else (self.inner, self.inner_keys)
        )
        for row in build.rows():
            self.build_rows += 1
            key = tuple(k(row) for k in build_keys)
            if None not in key:
                self._table.setdefault(key, []).append(row)
        self._probe = self.inner if self.build_outer else self.outer
        self._probe.open()
        self._pending = iter(())

    def next(self) -> Row | None:
        while True:
            row = next(self._pending, None)
            if row is not None:
                return row
            probe_row = self._probe.next()
            if probe_row is None:
                return None
            if self.build_outer:  # probing with inner rows; output is still outer + inner
                key = tuple(k(probe_row) for k in self.inner_keys)
                found = [] if None in key else self._table.get(key, [])
                rows = [outer_row + probe_row for outer_row in found]
                if self.residual is not None:
                    rows = [r for r in rows if self.residual(r) is True]
                self._pending = iter(rows)
            else:
                self._pending = iter(self._matches(probe_row))

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
        self.inner.close()
        self._table = {}
