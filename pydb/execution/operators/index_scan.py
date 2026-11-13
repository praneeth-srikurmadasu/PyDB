"""Reads the rows of a table whose index key falls in one or more ranges."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from pydb.catalog.schema import IndexInfo, TableInfo
from pydb.execution.operators.base import RidSource
from pydb.storage.table_heap import RID
from pydb.storage.tuple import Row
from pydb.types import Value


@dataclass(frozen=True, slots=True)
class KeyRange:
    """Keys between ``low`` and ``high``; either may be None (unbounded) or a key prefix."""

    low: tuple[Value, ...] | None
    high: tuple[Value, ...] | None
    low_inclusive: bool = True
    high_inclusive: bool = True

    @classmethod
    def point(cls, key: tuple[Value, ...]) -> "KeyRange":
        return cls(key, key)

    def describe(self) -> str:
        if self.low is not None and self.low == self.high:
            return f"= {_key(self.low)}"
        parts = []
        if self.low is not None:
            parts.append(f"{'>=' if self.low_inclusive else '>'} {_key(self.low)}")
        if self.high is not None:
            parts.append(f"{'<=' if self.high_inclusive else '<'} {_key(self.high)}")
        return " AND ".join(parts) or "all"


def _key(values: tuple[Value, ...]) -> str:
    return repr(values[0]) if len(values) == 1 else repr(values)


class IndexScan(RidSource):
    """Rows found through ``index`` for each range in turn, in key order within a range.

    The ranges must not overlap (the planner builds them from distinct values), so no row is
    returned twice.
    """

    def __init__(self, table: TableInfo, index: IndexInfo, ranges: Sequence[KeyRange]) -> None:
        self.table = table
        self.index = index
        self.ranges = list(ranges)
        self.columns = tuple(table.column_names())
        self._items: Iterator[tuple[RID, Row]] = iter(())

    def label(self) -> str:
        ranges = " OR ".join(r.describe() for r in self.ranges)
        return f"IndexScan {self.table.name} using {self.index.name} ({ranges})"

    def open(self) -> None:
        self._items = self._generate()

    def _generate(self) -> Iterator[tuple[RID, Row]]:
        for key_range in self.ranges:
            for _, rid in self.index.tree.range_scan(
                key_range.low,
                key_range.high,
                low_inclusive=key_range.low_inclusive,
                high_inclusive=key_range.high_inclusive,
            ):
                yield rid, self.table.layout.deserialize(self.table.heap.get(rid))

    def next_with_rid(self) -> tuple[RID, Row] | None:
        return next(self._items, None)

    def close(self) -> None:
        self._items = iter(())
