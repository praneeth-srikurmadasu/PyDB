"""DELETE: removes matching rows from the heap and from every index."""

from pydb.catalog.schema import TableInfo
from pydb.execution.operators.base import Operator, RidSource
from pydb.execution.table_writer import Journal, TableWriter
from pydb.storage.tuple import Row


class Delete(Operator):
    columns = ("count",)

    def __init__(self, table: TableInfo, source: RidSource, journal: Journal | None = None) -> None:
        self.journal = journal
        self.table = table
        self.source = source
        self._done = False

    def label(self) -> str:
        return f"Delete {self.table.name}"

    def children(self) -> list[Operator]:
        return [self.source]

    def open(self) -> None:
        self._done = False

    def next(self) -> Row | None:
        if self._done:
            return None
        self._done = True
        self.source.open()
        try:
            targets = []
            while (item := self.source.next_with_rid()) is not None:
                targets.append(item)
        finally:
            self.source.close()
        writer = TableWriter(self.table, self.journal)
        for rid, row in targets:
            writer.delete(rid, row)
        return (len(targets),)
