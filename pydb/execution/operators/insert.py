"""INSERT: validates and writes rows, keeping every index in step."""

from collections.abc import Sequence

from pydb.catalog.schema import TableInfo
from pydb.execution.operators.base import Operator
from pydb.execution.table_writer import TableWriter
from pydb.storage.tuple import Row
from pydb.types import Value


class Insert(Operator):
    """Writes all rows on the first ``next()`` and returns ``(count,)``; then None."""

    columns = ("count",)

    def __init__(self, table: TableInfo, rows: Sequence[Sequence[Value]]) -> None:
        self.table = table
        self.input = [list(r) for r in rows]
        self._done = False

    def label(self) -> str:
        return f"Insert {self.table.name}"

    def open(self) -> None:
        self._done = False

    def next(self) -> Row | None:
        if self._done:
            return None
        self._done = True
        writer = TableWriter(self.table)
        try:
            for values in self.input:
                writer.insert(writer.prepare(values))
        except BaseException:
            writer.rollback()
            raise
        return (len(self.input),)
