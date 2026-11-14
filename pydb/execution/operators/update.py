"""UPDATE: rewrites matching rows, moving index entries when keys change."""

from collections.abc import Sequence

from pydb.catalog.schema import TableInfo
from pydb.execution.expressions import Evaluator
from pydb.execution.operators.base import Operator, RidSource
from pydb.execution.table_writer import Journal, TableWriter
from pydb.storage.tuple import Row


class Update(Operator):
    """Collects every target row first, then updates them.

    Collecting first avoids the Halloween problem: a row that moves to a later page during
    the update would otherwise be found again by the still-running scan and updated twice.
    """

    columns = ("count",)

    def __init__(
        self,
        table: TableInfo,
        source: RidSource,
        assignments: Sequence[tuple[int, Evaluator]],
        journal: Journal | None = None,
    ) -> None:
        self.journal = journal
        self.table = table
        self.source = source
        self.assignments = list(assignments)
        self._done = False

    def label(self) -> str:
        return f"Update {self.table.name}"

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
        for rid, old in targets:
            values = list(old)
            for position, expr in self.assignments:
                values[position] = expr(old)
            writer.update(rid, old, writer.prepare(values))
        return (len(targets),)
