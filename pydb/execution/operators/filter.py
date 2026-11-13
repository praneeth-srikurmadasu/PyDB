"""Keeps the rows for which a predicate is TRUE (not FALSE, not NULL)."""

from pydb.execution.expressions import Evaluator
from pydb.execution.operators.base import Operator, RidSource
from pydb.storage.table_heap import RID
from pydb.storage.tuple import Row


class Filter(RidSource):
    def __init__(self, child: Operator, predicate: Evaluator) -> None:
        self.child = child
        self.predicate = predicate
        self.columns = child.columns

    def open(self) -> None:
        self.child.open()

    def next(self) -> Row | None:
        while (row := self.child.next()) is not None:
            if self.predicate(row) is True:
                return row
        return None

    def next_with_rid(self) -> tuple[RID, Row] | None:
        if not isinstance(self.child, RidSource):
            raise TypeError("Filter.next_with_rid needs a child that tracks RIDs")
        while (item := self.child.next_with_rid()) is not None:
            if self.predicate(item[1]) is True:
                return item
        return None

    def close(self) -> None:
        self.child.close()
