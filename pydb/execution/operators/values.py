"""A fixed list of rows: the input of SELECT without FROM."""

from collections.abc import Sequence

from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row


class Values(Operator):
    def __init__(self, rows: Sequence[Row], columns: Sequence[str] = ()) -> None:
        self._source = list(rows)
        self.columns = tuple(columns)
        self._position = 0

    def open(self) -> None:
        self._position = 0

    def next(self) -> Row | None:
        if self._position >= len(self._source):
            return None
        row = self._source[self._position]
        self._position += 1
        return row
