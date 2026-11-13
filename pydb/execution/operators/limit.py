"""LIMIT / OFFSET."""

from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row


class Limit(Operator):
    """Skips ``offset`` rows, then returns at most ``limit`` rows (all if None). It stops
    pulling from its child as soon as the limit is reached."""

    def __init__(self, child: Operator, limit: int | None, offset: int = 0) -> None:
        self.child = child
        self.limit = limit
        self.offset = offset
        self.columns = child.columns
        self._emitted = 0

    def open(self) -> None:
        self.child.open()
        self._emitted = 0
        for _ in range(self.offset):
            if self.child.next() is None:
                break

    def next(self) -> Row | None:
        if self.limit is not None and self._emitted >= self.limit:
            return None
        row = self.child.next()
        if row is not None:
            self._emitted += 1
        return row

    def close(self) -> None:
        self.child.close()
