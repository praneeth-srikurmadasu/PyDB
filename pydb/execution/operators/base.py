"""The Volcano iterator interface shared by every operator."""

from abc import ABC, abstractmethod
from collections.abc import Iterator

from pydb.storage.table_heap import RID
from pydb.storage.tuple import Row


class Operator(ABC):
    """Produces rows one at a time: ``open()``, then ``next()`` until None, then ``close()``.

    ``columns`` names the values in each output row.
    """

    columns: tuple[str, ...]
    est_rows: float = 0.0
    """Rows the planner expects this operator to produce."""
    est_cost: float = 0.0
    """The planner's cost estimate for producing all rows, including the children's cost."""

    def label(self) -> str:
        """One line describing the operator, for EXPLAIN."""
        return type(self).__name__

    def children(self) -> list["Operator"]:
        return []

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def next(self) -> Row | None: ...

    def close(self) -> None:  # noqa: B027  (optional hook; most operators have nothing to free)
        """Release resources. Safe to call more than once."""

    def rows(self) -> Iterator[Row]:
        """Run the operator to completion, closing it even if the consumer stops early."""
        self.open()
        try:
            while (row := self.next()) is not None:
                yield row
        finally:
            self.close()


class RidSource(Operator):
    """An operator over one table that can also say where each row lives (for UPDATE/DELETE)."""

    @abstractmethod
    def next_with_rid(self) -> tuple[RID, Row] | None: ...

    def next(self) -> Row | None:
        item = self.next_with_rid()
        return None if item is None else item[1]
