"""Turns a bound SELECT into a tree of operators.

The pipeline, bottom to top::

    SeqScan -> Join ... -> Filter(WHERE) -> HashAggregate -> Filter(HAVING) -> Projection
            -> Distinct -> Sort | TopN -> Limit -> Projection (drops hidden sort columns)

Join algorithm, per join, unless ``PlannerSettings.join_strategy`` forces one:

1. **index nested loop** when the ON clause equates outer expressions with the leading
   column(s) of an index on the inner table;
2. otherwise **hash join** when the ON clause has at least one equality between an outer
   expression and an inner expression;
3. otherwise **nested loop**.

A forced strategy that does not apply to a join falls back to the next one in that list.
"""

from dataclasses import dataclass

from pydb.catalog.schema import IndexInfo, TableInfo
from pydb.config import SORT_MEMORY_BYTES
from pydb.execution.expressions import (
    BoundBinary,
    BoundColumn,
    BoundExpr,
    Evaluator,
    and_all,
    column_refs,
    compile_expr,
    conjuncts,
    shift_columns,
)
from pydb.execution.operators.aggregate import AggregateSpec, HashAggregate
from pydb.execution.operators.base import Operator, RidSource
from pydb.execution.operators.distinct import Distinct
from pydb.execution.operators.filter import Filter
from pydb.execution.operators.hash_join import HashJoin
from pydb.execution.operators.index_nl_join import IndexNestedLoopJoin
from pydb.execution.operators.limit import Limit
from pydb.execution.operators.nested_loop_join import NestedLoopJoin
from pydb.execution.operators.projection import Projection
from pydb.execution.operators.seq_scan import SeqScan
from pydb.execution.operators.sort import Sort, TopN
from pydb.execution.operators.values import Values
from pydb.planner.binder import BoundJoin, BoundSelect
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.types import DataType

JOIN_STRATEGIES = ("auto", "index_nested_loop", "hash", "nested_loop")


@dataclass(frozen=True, slots=True)
class PlannerSettings:
    join_strategy: str = "auto"
    sort_memory_bytes: int = SORT_MEMORY_BYTES

    def __post_init__(self) -> None:
        if self.join_strategy not in JOIN_STRATEGIES:
            raise ValueError(f"join_strategy must be one of {JOIN_STRATEGIES}")
        if self.sort_memory_bytes < 1:
            raise ValueError("sort_memory_bytes must be positive")


def table_source(table: TableInfo, where: BoundExpr | None) -> RidSource:
    scan = SeqScan(table)
    return scan if where is None else Filter(scan, compile_expr(where))


def build_select(
    bound: BoundSelect, pool: BufferPoolManager, settings: PlannerSettings
) -> Operator:
    plan: Operator
    if bound.table is None:
        plan = Values([()])
    else:
        plan = SeqScan(bound.table)
        for join in bound.joins:
            plan = plan_join(plan, join, settings)
    if bound.where is not None:
        plan = Filter(plan, compile_expr(bound.where))
    if bound.grouped:
        specs = [
            AggregateSpec(a.function, None if a.arg is None else compile_expr(a.arg))
            for a in bound.aggregates
        ]
        names = [f"group{i}" for i in range(len(bound.group_by))]
        names += [a.function.lower() for a in bound.aggregates]
        plan = HashAggregate(plan, [compile_expr(g) for g in bound.group_by], specs, names)
        if bound.having is not None:
            plan = Filter(plan, compile_expr(bound.having))

    names = [name for name, _ in bound.outputs] + [f"_sort{i}" for i in range(len(bound.hidden))]
    exprs = [e for _, e in bound.outputs] + list(bound.hidden)
    plan = Projection(plan, names, [compile_expr(e) for e in exprs])
    if bound.distinct:
        plan = Distinct(plan)

    offset = bound.offset or 0
    if bound.order_by:
        if bound.limit is not None:
            plan = TopN(plan, bound.order_by, bound.limit + offset)
        else:
            plan = Sort(
                plan, bound.order_by, bound.projected_types, pool, settings.sort_memory_bytes
            )
    if bound.limit is not None or offset:
        plan = Limit(plan, bound.limit, offset)
    if bound.hidden:
        visible = len(bound.outputs)
        plan = Projection(
            plan,
            names[:visible],
            [compile_expr(BoundColumn(i, e.type, n)) for i, (n, e) in enumerate(bound.outputs)],
        )
    return plan


def plan_join(outer: Operator, join: BoundJoin, settings: PlannerSettings) -> Operator:
    width = join.left_width
    equalities: list[tuple[BoundExpr, BoundExpr]] = []  # (outer expr, inner expr over inner row)
    residual: list[BoundExpr] = []
    for condition in conjuncts(join.on):
        pair = _split_equality(condition, width)
        if pair is None:
            residual.append(condition)
        else:
            equalities.append(pair)

    strategy = settings.join_strategy
    if strategy in ("auto", "index_nested_loop"):
        chosen = _usable_index(join.table, equalities)
        if chosen is not None:
            index, used = chosen
            outer_keys = [compile_expr(equalities[i][0]) for i in used]
            leftover = [
                _rebuild_equality(equalities[i], width)
                for i in range(len(equalities))
                if i not in used
            ]
            return IndexNestedLoopJoin(
                outer, join.table, index, outer_keys, _compiled(residual + leftover), join.kind
            )
    if strategy in ("auto", "index_nested_loop", "hash") and equalities:
        return HashJoin(
            outer,
            SeqScan(join.table),
            [compile_expr(o) for o, _ in equalities],
            [compile_expr(i) for _, i in equalities],
            _compiled(residual),
            join.kind,
        )
    return NestedLoopJoin(outer, SeqScan(join.table), compile_expr(join.on), join.kind)


def _split_equality(condition: BoundExpr, width: int) -> tuple[BoundExpr, BoundExpr] | None:
    """If ``condition`` is ``outer_expr = inner_expr`` (either way round), return the pair
    with the inner expression rewritten to read the inner row on its own."""
    if not (isinstance(condition, BoundBinary) and condition.op == "="):
        return None
    left_refs, right_refs = column_refs(condition.left), column_refs(condition.right)

    def outer_only(refs: set[int]) -> bool:
        return bool(refs) and max(refs) < width

    def inner_only(refs: set[int]) -> bool:
        return bool(refs) and min(refs) >= width

    if outer_only(left_refs) and inner_only(right_refs):
        return condition.left, shift_columns(condition.right, -width)
    if inner_only(left_refs) and outer_only(right_refs):
        return condition.right, shift_columns(condition.left, -width)
    return None


def _rebuild_equality(pair: tuple[BoundExpr, BoundExpr], width: int) -> BoundExpr:
    outer, inner = pair
    return BoundBinary("=", outer, shift_columns(inner, width), DataType.BOOL)


def _usable_index(
    table: TableInfo, equalities: list[tuple[BoundExpr, BoundExpr]]
) -> tuple[IndexInfo, list[int]] | None:
    """The index matching the most leading columns with ``inner_column = outer_expr``.

    Returns the index and, for each matched leading column in order, the position of its
    equality in ``equalities``.
    """
    by_column: dict[int, int] = {}
    for i, (_, inner) in enumerate(equalities):
        if isinstance(inner, BoundColumn) and inner.index not in by_column:
            by_column[inner.index] = i
    best: tuple[IndexInfo, list[int]] | None = None
    for index in table.indexes:
        used: list[int] = []
        for column in index.columns:
            if column not in by_column:
                break
            used.append(by_column[column])
        if used and (best is None or len(used) > len(best[1])):
            best = (index, used)
    return best


def _compiled(conditions: list[BoundExpr]) -> Evaluator | None:
    combined = and_all(conditions)
    return None if combined is None else compile_expr(combined)
