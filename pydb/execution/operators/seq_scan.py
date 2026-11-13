"""Full table scan."""

from collections.abc import Iterator

from pydb.catalog.schema import TableInfo
from pydb.execution.operators.base import RidSource
from pydb.storage.table_heap import RID
from pydb.storage.tuple import Row


class SeqScan(RidSource):
    """Every live row of a table, in heap order."""

    def __init__(self, table: TableInfo) -> None:
        self.table = table
        self.columns = tuple(table.column_names())
        self._rows: Iterator[tuple[RID, bytes]] | None = None

    def open(self) -> None:
        self._rows = self.table.heap.scan()

    def next_with_rid(self) -> tuple[RID, Row] | None:
        if self._rows is None:
            raise RuntimeError("SeqScan.next() called before open()")
        item = next(self._rows, None)
        if item is None:
            return None
        rid, data = item
        return rid, self.table.layout.deserialize(data)

    def close(self) -> None:
        self._rows = None
