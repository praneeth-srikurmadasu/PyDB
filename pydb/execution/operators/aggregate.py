"""GROUP BY with aggregate functions, using a hash table of groups."""

from collections.abc import Sequence
from dataclasses import dataclass

from pydb.errors import ValueTooLargeError
from pydb.execution.expressions import Evaluator
from pydb.execution.operators.base import Operator
from pydb.storage.tuple import Row
from pydb.types import INT_MAX, INT_MIN, Value


@dataclass(frozen=True, slots=True)
class AggregateSpec:
    function: str  # COUNT, SUM, AVG, MIN, MAX
    arg: Evaluator | None  # None for COUNT(*)


class _Accumulator:
    __slots__ = ("best", "count", "spec", "total")

    def __init__(self, spec: AggregateSpec) -> None:
        self.spec = spec
        self.count = 0
        self.total: int | float = 0
        self.best: Value = None

    def add(self, row: Row) -> None:
        spec = self.spec
        if spec.arg is None:
            self.count += 1
            return
        value = spec.arg(row)
        if value is None:  # aggregates skip NULLs
            return
        self.count += 1
        if spec.function in ("SUM", "AVG"):
            assert isinstance(value, int | float)
            self.total += value
        elif spec.function == "MIN":
            if self.best is None or value < self.best:  # type: ignore[operator]
                self.best = value
        elif spec.function == "MAX" and (self.best is None or value > self.best):  # type: ignore[operator]
            self.best = value

    def result(self) -> Value:
        function = self.spec.function
        if function == "COUNT":
            return self.count
        if self.count == 0:
            return None  # SUM/AVG/MIN/MAX of no values is NULL
        if function == "SUM":
            if isinstance(self.total, int) and not INT_MIN <= self.total <= INT_MAX:
                raise ValueTooLargeError(f"SUM out of the INT range: {self.total}")
            return self.total
        if function == "AVG":
            return self.total / self.count
        return self.best


class HashAggregate(Operator):
    """Groups input rows by ``group_keys`` and computes ``aggregates`` per group.

    Output rows are the group-key values followed by the aggregate results, in the order
    groups first appear. Without group keys the whole input is one group, so an empty input
    still produces one row (COUNT 0, other aggregates NULL); with group keys it produces none.
    NULL group keys form their own group.
    """

    def __init__(
        self,
        child: Operator,
        group_keys: Sequence[Evaluator],
        aggregates: Sequence[AggregateSpec],
        columns: Sequence[str],
    ) -> None:
        self.child = child
        self.group_keys = list(group_keys)
        self.aggregates = list(aggregates)
        self.columns = tuple(columns)
        self._output: list[Row] = []
        self._position = 0

    def open(self) -> None:
        groups: dict[tuple[Value, ...], list[_Accumulator]] = {}
        if not self.group_keys:
            groups[()] = [_Accumulator(spec) for spec in self.aggregates]
        for row in self.child.rows():
            key = tuple(k(row) for k in self.group_keys)
            accumulators = groups.get(key)
            if accumulators is None:
                accumulators = groups[key] = [_Accumulator(spec) for spec in self.aggregates]
            for accumulator in accumulators:
                accumulator.add(row)
        self._output = [key + tuple(a.result() for a in accs) for key, accs in groups.items()]
        self._position = 0

    def next(self) -> Row | None:
        if self._position >= len(self._output):
            return None
        row = self._output[self._position]
        self._position += 1
        return row

    def close(self) -> None:
        self._output = []
