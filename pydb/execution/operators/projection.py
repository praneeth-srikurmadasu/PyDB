"""Computes the output columns of a query from each input row."""

from collections.abc import Sequence

from pydb.execution.expressions import Evaluator
from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row


class Projection(Operator):
    def __init__(self, child: Operator, names: Sequence[str], exprs: Sequence[Evaluator]) -> None:
        self.child = child
        self.columns = tuple(names)
        self.exprs = list(exprs)

    def label(self) -> str:
        return f"Projection [{', '.join(self.columns)}]"

    def children(self) -> list[Operator]:
        return [self.child]

    def open(self) -> None:
        self.child.open()

    def next(self) -> Row | None:
        row = self.child.next()
        if row is None:
            return None
        return tuple(expr(row) for expr in self.exprs)

    def close(self) -> None:
        self.child.close()
