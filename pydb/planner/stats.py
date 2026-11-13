"""Table statistics and selectivity estimation.

``ANALYZE t`` computes a ``TableStats``: the row and page counts and, per column, the number
of distinct non-NULL values, the number of NULLs, and the minimum and maximum. The planner
estimates what fraction of rows a predicate keeps (its *selectivity*) with the System R
rules:

=========================  ============================================  ===========
predicate                  with statistics                               without
=========================  ============================================  ===========
``col = constant``         1 / distinct                                  1/10
``col1 = col2`` (join)     1 / max(distinct1, distinct2)                 1/10
``col < constant`` etc.    (constant - min) / (max - min), numbers only  1/3
``col BETWEEN a AND b``    (b - a) / (max - min), numbers only           1/3
``col IN (k values)``      k / distinct, at most 1                       k/10
``col IS NULL``            nulls / rows                                  1/3
``a AND b``                s(a) * s(b)
``a OR b``                 s(a) + s(b) - s(a) * s(b)
``NOT a``                  1 - s(a)
anything else                                                            1/3
=========================  ============================================  ===========
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydb.catalog.schema import TableInfo
from pydb.execution.expressions import (
    BoundBetween,
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundInList,
    BoundIsNull,
    BoundLiteral,
    BoundUnary,
)
from pydb.types import Value

DEFAULT_ROWS = 1000
DEFAULT_ROWS_PER_PAGE = 50
EQUALITY_DEFAULT = 0.1
OTHER_DEFAULT = 1 / 3


@dataclass(frozen=True, slots=True)
class ColumnStats:
    distinct: int
    nulls: int
    min: Value
    max: Value


@dataclass(frozen=True, slots=True)
class TableStats:
    row_count: int
    page_count: int
    columns: tuple[ColumnStats, ...]


def collect(table: TableInfo) -> TableStats:
    """Scan ``table`` once and compute its statistics."""
    width = len(table.columns)
    distinct: list[set[Value]] = [set() for _ in range(width)]
    nulls = [0] * width
    rows = 0
    for _, data in table.heap.scan():
        rows += 1
        for i, value in enumerate(table.layout.deserialize(data)):
            if value is None:
                nulls[i] += 1
            else:
                distinct[i].add(value)
    columns = tuple(
        ColumnStats(
            len(values),
            nulls[i],
            min(values) if values else None,  # type: ignore[type-var]
            max(values) if values else None,  # type: ignore[type-var]
        )
        for i, values in enumerate(distinct)
    )
    return TableStats(rows, len(list(table.heap.page_ids())), columns)


def table_size(table: TableInfo) -> tuple[float, float]:
    """Estimated ``(rows, pages)``: from statistics, or defaults before ANALYZE."""
    if table.stats is not None:
        return float(table.stats.row_count), float(max(table.stats.page_count, 1))
    return float(DEFAULT_ROWS), float(DEFAULT_ROWS // DEFAULT_ROWS_PER_PAGE)


ColumnLookup = Callable[[int], tuple[ColumnStats | None, float]]
"""Maps an input column position to its statistics (or None) and its table's row count."""


def selectivity(expr: BoundExpr, lookup: ColumnLookup) -> float:
    """Estimated fraction of rows for which ``expr`` is TRUE, between 0 and 1."""
    return max(0.0, min(1.0, _selectivity(expr, lookup)))


def _selectivity(expr: BoundExpr, lookup: ColumnLookup) -> float:
    match expr:
        case BoundLiteral(value=value):
            return 1.0 if value is True else 0.0
        case BoundBinary(op="AND", left=left, right=right):
            return selectivity(left, lookup) * selectivity(right, lookup)
        case BoundBinary(op="OR", left=left, right=right):
            a, b = selectivity(left, lookup), selectivity(right, lookup)
            return a + b - a * b
        case BoundUnary(op="NOT", operand=operand):
            return 1.0 - selectivity(operand, lookup)
        case BoundBinary(op="=", left=BoundColumn() as a, right=BoundColumn() as b):
            stats_a, _ = lookup(a.index)
            stats_b, _ = lookup(b.index)
            if stats_a is None or stats_b is None:
                return EQUALITY_DEFAULT
            return 1.0 / max(stats_a.distinct, stats_b.distinct, 1)
        case BoundBinary(op=op, left=left, right=right) if op in ("=", "!=", "<", "<=", ">", ">="):
            column, constant, op = _column_vs_constant(left, right, op)
            if column is None:
                return OTHER_DEFAULT
            stats, rows = lookup(column.index)
            if op == "=":
                return EQUALITY_DEFAULT if stats is None else 1.0 / max(stats.distinct, 1)
            if op == "!=":
                eq = EQUALITY_DEFAULT if stats is None else 1.0 / max(stats.distinct, 1)
                return 1.0 - eq
            return _range_fraction(stats, constant, op)
        case BoundBetween(operand=BoundColumn() as column, low=BoundLiteral() as lo,
                          high=BoundLiteral() as hi, negated=negated):  # fmt: skip
            stats, _ = lookup(column.index)
            inside = _between_fraction(stats, lo.value, hi.value)
            return 1.0 - inside if negated else inside
        case BoundInList(operand=BoundColumn() as column, items=items, negated=negated):
            stats, _ = lookup(column.index)
            each = EQUALITY_DEFAULT if stats is None else 1.0 / max(stats.distinct, 1)
            inside = min(1.0, each * len(set(items)))
            return 1.0 - inside if negated else inside
        case BoundIsNull(operand=BoundColumn() as column, negated=negated):
            stats, rows = lookup(column.index)
            if stats is None or rows == 0:
                return OTHER_DEFAULT
            fraction = stats.nulls / rows
            return 1.0 - fraction if negated else fraction
    return OTHER_DEFAULT


_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "!=": "!="}


def _column_vs_constant(
    left: BoundExpr, right: BoundExpr, op: str
) -> tuple[BoundColumn | None, Value, str]:
    if isinstance(left, BoundColumn) and isinstance(right, BoundLiteral):
        return left, right.value, op
    if isinstance(right, BoundColumn) and isinstance(left, BoundLiteral):
        return right, left.value, _FLIP[op]
    return None, None, op


def _numeric(value: Value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _range_fraction(stats: ColumnStats | None, constant: Value, op: str) -> float:
    if stats is None:
        return OTHER_DEFAULT
    c, lo, hi = _numeric(constant), _numeric(stats.min), _numeric(stats.max)
    if c is None or lo is None or hi is None:
        return OTHER_DEFAULT
    if hi == lo:
        below = 1.0 if c > lo else 0.0
        return below if op in ("<", "<=") else 1.0 - below
    fraction = (c - lo) / (hi - lo)
    return fraction if op in ("<", "<=") else 1.0 - fraction


def _between_fraction(stats: ColumnStats | None, low: Value, high: Value) -> float:
    if stats is None:
        return OTHER_DEFAULT
    a, b = _numeric(low), _numeric(high)
    lo, hi = _numeric(stats.min), _numeric(stats.max)
    if a is None or b is None or lo is None or hi is None:
        return OTHER_DEFAULT
    if hi == lo:
        return 1.0 if a <= lo <= b else 0.0
    return max(0.0, min(b, hi) - max(a, lo)) / (hi - lo)
