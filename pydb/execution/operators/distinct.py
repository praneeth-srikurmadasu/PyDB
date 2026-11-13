"""Removes duplicate rows, keeping the first occurrence."""

from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row
from pydb.types import Value


class Distinct(Operator):
    """Streams rows, remembering each distinct row in a hash set. NULLs compare equal here,
    as SQL's DISTINCT treats them as the same value."""

    def __init__(self, child: Operator) -> None:
        self.child = child
        self.columns = child.columns
        self._seen: set[tuple[Value, ...]] = set()

    def open(self) -> None:
        self._seen = set()
        self.child.open()

    def next(self) -> Row | None:
        while (row := self.child.next()) is not None:
            if row not in self._seen:
                self._seen.add(row)
                return row
        return None

    def close(self) -> None:
        self.child.close()
        self._seen = set()
