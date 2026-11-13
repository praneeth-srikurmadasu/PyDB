"""Rule- and cost-based query optimisation, producing physical operator trees.

The optimizer works on a bound query and applies, in order:

1. **Constant folding.** Sub-expressions without column references are evaluated once
   (``2 * 5`` becomes ``10``), and ``AND``/``OR`` with a constant side are simplified. An
   expression whose evaluation fails (``1 / 0``) is left as it is, so the error is raised
   only if a row actually reaches it.
2. **Predicate pushdown.** ``WHERE`` and ``ON`` are split into their ``AND``ed parts. A part
   that reads one table is applied when that table is scanned, below any join. For queries
   with LEFT joins, a ``WHERE`` part about a table on the NULL-padded side of a LEFT join stays
   above the joins (pushing it would turn NULL-padded rows into missing rows), while an
   ``ON`` part about the inner table is pushed into that table's scan.
3. **Access paths.** Each table is read by a sequential scan or by an index scan on an
   equality prefix plus an optional range (or an ``IN`` list) over an index's leading
   columns, chosen when its estimated selectivity is below ``INDEX_SELECTIVITY_THRESHOLD``.
4. **Join algorithm.** For each join, the cheapest applicable of index nested loop, hash
   join (building on the smaller input) and nested loop, by the cost model in
   ``physical_plan``. ``PlannerSettings.join_strategy`` can force one.
5. **Join order.** Without LEFT joins, every left-deep order of up to
   ``MAX_REORDERED_TABLES`` tables is costed and the cheapest kept; otherwise tables are joined
   in the order written.
6. **ORDER BY ... LIMIT** becomes a TopN operator.
"""

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydb.catalog.schema import IndexInfo, TableInfo
from pydb.config import MAX_KEY_TEXT_BYTES
from pydb.errors import PyDBError
from pydb.execution.expressions import (
    BoundBetween,
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundInList,
    BoundLiteral,
    and_all,
    column_refs,
    compile_expr,
    conjuncts,
    map_children,
    map_columns,
    render,
    shift_columns,
)
from pydb.execution.operators.aggregate import AggregateSpec, HashAggregate
from pydb.execution.operators.base import Operator, RidSource
from pydb.execution.operators.distinct import Distinct
from pydb.execution.operators.filter import Filter
from pydb.execution.operators.hash_join import HashJoin
from pydb.execution.operators.index_nl_join import IndexNestedLoopJoin
from pydb.execution.operators.index_scan import IndexScan, KeyRange
from pydb.execution.operators.limit import Limit
from pydb.execution.operators.nested_loop_join import NestedLoopJoin
from pydb.execution.operators.projection import Projection
from pydb.execution.operators.seq_scan import SeqScan
from pydb.execution.operators.sort import Sort, TopN
from pydb.execution.operators.values import Values
from pydb.planner.binder import BoundSelect
from pydb.planner.physical_plan import (
    CPU_ROW,
    HASH_ROW,
    INDEX_SELECTIVITY_THRESHOLD,
    MAX_REORDERED_TABLES,
    PlannerSettings,
)
from pydb.planner.stats import ColumnLookup, ColumnStats, selectivity, table_size
from pydb.storage.buffer_pool import BufferPoolManager
from pydb.types import DataType, Value

# --------------------------------------------------------------------- constant folding


def fold_constants(expr: BoundExpr) -> BoundExpr:
    """Evaluate constant sub-expressions and simplify AND/OR with a constant operand."""
    match expr:
        case BoundBinary(op="AND" | "OR" as op, left=left, right=right):
            a, b = fold_constants(left), fold_constants(right)
            absorbing, neutral = (False, True) if op == "AND" else (True, False)
            for this, other in ((a, b), (b, a)):
                if isinstance(this, BoundLiteral) and this.value is absorbing:
                    return BoundLiteral(absorbing, DataType.BOOL)
                if isinstance(this, BoundLiteral) and this.value is neutral:
                    return other
            expr = BoundBinary(op, a, b, DataType.BOOL)
        case BoundColumn() | BoundLiteral():
            return expr
        case _:
            expr = map_children(expr, fold_constants)
    if not column_refs(expr) and not isinstance(expr, BoundLiteral):
        try:
            return BoundLiteral(compile_expr(expr)(()), expr.type)
        except PyDBError:
            return expr
    return expr


# --------------------------------------------------------------------- relations


@dataclass(frozen=True, slots=True)
class Relation:
    """One table of a FROM clause, with where its columns sit in the bound (written) row."""

    number: int
    table: TableInfo
    offset: int
    kind: str  # "BASE", "INNER" or "LEFT"

    @property
    def width(self) -> int:
        return len(self.table.columns)


@dataclass(frozen=True, slots=True)
class _Condition:
    expr: BoundExpr  # over the written row
    relations: frozenset[int]


def _lookup_for(relations: Sequence[Relation]) -> ColumnLookup:
    """Statistics lookup for a row made of ``relations`` in this order."""
    starts: list[tuple[int, Relation]] = []
    position = 0
    for relation in relations:
        starts.append((position, relation))
        position += relation.width

    def lookup(index: int) -> tuple[ColumnStats | None, float]:
        for start, relation in starts:
            if start <= index < start + relation.width:
                rows, _ = table_size(relation.table)
                stats = relation.table.stats
                return (None if stats is None else stats.columns[index - start]), rows
        return None, 0.0

    return lookup


# --------------------------------------------------------------------- the optimizer


class Optimizer:
    def __init__(self, pool: BufferPoolManager, settings: PlannerSettings) -> None:
        self.pool = pool
        self.settings = settings

    # ----------------------------------------------------------------- single tables

    def access_path(self, table: TableInfo, where: BoundExpr | None) -> RidSource:
        """The cheapest way to read the rows of ``table`` satisfying ``where``."""
        conditions = [] if where is None else conjuncts(fold_constants(where))
        return self._access(table, conditions)

    def _access(self, table: TableInfo, conditions: list[BoundExpr]) -> RidSource:
        rows, pages = table_size(table)
        lookup = _lookup_for([Relation(0, table, 0, "BASE")])
        source: RidSource
        residual = list(conditions)
        candidate = (
            self._best_index(table, conditions, lookup) if self.settings.use_indexes else None
        )
        if candidate is not None and candidate.selectivity < INDEX_SELECTIVITY_THRESHOLD:
            source = IndexScan(table, candidate.index, candidate.ranges)
            matches = rows * candidate.selectivity
            source.est_rows = matches
            source.est_cost = candidate.index.tree.height() + matches * (1 + CPU_ROW)
            residual = [c for c in conditions if c not in candidate.used]
        else:
            source = SeqScan(table)
            source.est_rows = rows
            source.est_cost = pages + rows * CPU_ROW
        if not residual:
            return source
        predicate = and_all(residual)
        assert predicate is not None
        result = Filter(source, compile_expr(predicate), render(predicate))
        result.est_rows = rows * selectivity(and_all(conditions) or predicate, lookup)
        result.est_cost = source.est_cost + source.est_rows * CPU_ROW
        return result

    def _best_index(
        self, table: TableInfo, conditions: list[BoundExpr], lookup: ColumnLookup
    ) -> "_IndexCandidate | None":
        best: _IndexCandidate | None = None
        for index in table.indexes:
            candidate = _index_candidate(table, index, conditions, lookup)
            if candidate is not None and (best is None or candidate.selectivity < best.selectivity):
                best = candidate
        return best

    # ----------------------------------------------------------------- SELECT

    def plan_select(self, bound: BoundSelect) -> Operator:
        if bound.table is None:
            plan: Operator = Values([()])
            plan.est_rows, plan.est_cost = 1.0, 0.0
            final_order: list[Relation] = []
            remap: Callable[[int], int] = _identity
            top: list[BoundExpr] = (
                [] if bound.where is None else conjuncts(fold_constants(bound.where))
            )
        else:
            plan, final_order, remap, top = self._plan_from(bound)
        lookup = _lookup_for(final_order)

        if top:
            predicate = and_all([map_columns(c, remap) for c in top])
            assert predicate is not None
            filtered = Filter(plan, compile_expr(predicate), render(predicate))
            filtered.est_rows = plan.est_rows * selectivity(predicate, lookup)
            filtered.est_cost = plan.est_cost + plan.est_rows * CPU_ROW
            plan = filtered

        def stage_one(expr: BoundExpr) -> BoundExpr:
            return fold_constants(map_columns(expr, remap))

        if bound.grouped:
            group_by = [stage_one(g) for g in bound.group_by]
            specs = [
                AggregateSpec(a.function, None if a.arg is None else compile_expr(stage_one(a.arg)))
                for a in bound.aggregates
            ]
            names = [render(g) for g in group_by] + [
                f"{a.function}({'*' if a.arg is None else render(stage_one(a.arg))})"
                for a in bound.aggregates
            ]
            aggregate = HashAggregate(plan, [compile_expr(g) for g in group_by], specs, names)
            aggregate.est_rows = _group_estimate(group_by, plan.est_rows, lookup)
            aggregate.est_cost = plan.est_cost + plan.est_rows * HASH_ROW
            plan = aggregate
            if bound.having is not None:
                having = fold_constants(bound.having)
                filtered = Filter(plan, compile_expr(having), render(having))
                filtered.est_rows = max(1.0, plan.est_rows / 3)
                filtered.est_cost = plan.est_cost + plan.est_rows * CPU_ROW
                plan = filtered

        def stage_two(expr: BoundExpr) -> BoundExpr:
            return fold_constants(expr) if bound.grouped else stage_one(expr)

        exprs = [stage_two(e) for _, e in bound.outputs] + [stage_two(e) for e in bound.hidden]
        names = [name for name, _ in bound.outputs] + [
            f"_sort{i}" for i in range(len(bound.hidden))
        ]
        plan = _with_estimate(Projection(plan, names, [compile_expr(e) for e in exprs]), plan)
        if bound.distinct:
            plan = _with_estimate(Distinct(plan), plan)

        offset = bound.offset or 0
        rows = plan.est_rows
        if bound.order_by:
            if bound.limit is not None:
                count = bound.limit + offset
                top_n = TopN(plan, bound.order_by, count)
                top_n.est_rows = min(rows, count)
                top_n.est_cost = plan.est_cost + rows * math.log2(count + 2) * CPU_ROW
                plan = top_n
            else:
                sort = Sort(
                    plan,
                    bound.order_by,
                    bound.projected_types,
                    self.pool,
                    self.settings.sort_memory_bytes,
                )
                sort.est_rows = rows
                sort.est_cost = plan.est_cost + rows * math.log2(rows + 2) * CPU_ROW
                plan = sort
        if bound.limit is not None or offset:
            limited = Limit(plan, bound.limit, offset)
            remaining = max(plan.est_rows - offset, 0.0)
            limited.est_rows = remaining if bound.limit is None else min(remaining, bound.limit)
            limited.est_cost = plan.est_cost
            plan = limited
        if bound.hidden:
            visible = [
                compile_expr(BoundColumn(i, e.type, n)) for i, (n, e) in enumerate(bound.outputs)
            ]
            plan = _with_estimate(Projection(plan, names[: len(visible)], visible), plan)
        return plan

    # ----------------------------------------------------------------- FROM clause

    def _plan_from(
        self, bound: BoundSelect
    ) -> tuple[Operator, list[Relation], Callable[[int], int], list[BoundExpr]]:
        assert bound.table is not None
        relations = [Relation(0, bound.table, 0, "BASE")]
        for number, join in enumerate(bound.joins, start=1):
            relations.append(Relation(number, join.table, join.left_width, join.kind))

        def owners(expr: BoundExpr) -> frozenset[int]:
            return frozenset(
                r.number
                for r in relations
                for i in column_refs(expr)
                if r.offset <= i < r.offset + r.width
            )

        where = [] if bound.where is None else conjuncts(fold_constants(bound.where))
        local: dict[int, list[BoundExpr]] = {r.number: [] for r in relations}
        join_conditions: list[_Condition] = []  # INNER-only queries
        on_conditions: dict[int, list[BoundExpr]] = {r.number: [] for r in relations}
        top: list[BoundExpr] = []
        has_left = any(r.kind == "LEFT" for r in relations)

        if not has_left:
            pool_of_conditions = where + [
                c for join in bound.joins for c in conjuncts(fold_constants(join.on))
            ]
            for expr in pool_of_conditions:
                found = owners(expr)
                if not found:
                    top.append(expr)
                elif len(found) == 1:
                    local[next(iter(found))].append(expr)
                else:
                    join_conditions.append(_Condition(expr, found))
        else:
            nullable = {r.number for r in relations if r.kind == "LEFT"}
            for expr in where:
                found = owners(expr)
                if len(found) == 1 and not found & nullable:
                    local[next(iter(found))].append(expr)
                else:
                    top.append(expr)
            for number, join in enumerate(bound.joins, start=1):
                for expr in conjuncts(fold_constants(join.on)):
                    if owners(expr) == {number}:
                        local[number].append(expr)
                    else:
                        on_conditions[number].append(expr)

        orders: list[tuple[int, ...]]
        n = len(relations)
        if not has_left and self.settings.reorder_joins and 1 < n <= MAX_REORDERED_TABLES:
            orders = list(itertools.permutations(range(n)))
        else:
            orders = [tuple(range(n))]

        best: tuple[Operator, list[Relation]] | None = None
        for order in orders:
            plan, placed = self._build_order(
                [relations[i] for i in order], local, join_conditions, on_conditions, has_left
            )
            if best is None or plan.est_cost < best[0].est_cost:
                best = (plan, placed)
        assert best is not None
        plan, placed = best
        return plan, placed, _remapper(relations, placed), top

    def _build_order(
        self,
        order: list[Relation],
        local: dict[int, list[BoundExpr]],
        join_conditions: list[_Condition],
        on_conditions: dict[int, list[BoundExpr]],
        has_left: bool,
    ) -> tuple[Operator, list[Relation]]:
        first = order[0]
        plan: Operator = self._access(
            first.table, [shift_columns(c, -first.offset) for c in local[first.number]]
        )
        placed = [first]
        used: set[int] = set()
        for relation in order[1:]:
            present = {r.number for r in placed} | {relation.number}
            if has_left:
                conditions = on_conditions[relation.number]
            else:
                conditions = []
                for i, condition in enumerate(join_conditions):
                    if i not in used and condition.relations <= present:
                        used.add(i)
                        conditions.append(condition.expr)
            remap = _remapper([*placed, relation], [*placed, relation])
            plan = self._join(
                plan,
                placed,
                relation,
                [map_columns(c, remap) for c in conditions],
                [shift_columns(c, -relation.offset) for c in local[relation.number]],
                relation.kind if relation.kind != "BASE" else "INNER",
            )
            placed.append(relation)
        return plan, placed

    def _join(
        self,
        outer: Operator,
        placed: list[Relation],
        relation: Relation,
        conditions: list[BoundExpr],  # over the combined row: placed..., relation
        local: list[BoundExpr],  # over the relation's own row
        kind: str,
    ) -> Operator:
        width = sum(r.width for r in placed)
        lookup = _lookup_for([*placed, relation])
        inner = self._access(relation.table, local)
        equalities: list[tuple[BoundExpr, BoundExpr]] = []
        residual: list[BoundExpr] = []
        for condition in conditions:
            pair = _split_equality(condition, width)
            if pair is None:
                residual.append(condition)
            else:
                equalities.append(pair)
        on = and_all(conditions)
        join_selectivity = 1.0 if on is None else selectivity(on, lookup)
        out_rows = outer.est_rows * inner.est_rows * join_selectivity
        index_choice = _usable_index(relation.table, equalities)
        unique_inner = (
            index_choice is not None
            and index_choice[0].unique
            and len(index_choice[1]) == len(index_choice[0].columns)
        )
        if unique_inner:  # each outer row matches at most one inner row
            out_rows = min(out_rows, outer.est_rows)
        if kind == "LEFT":
            out_rows = max(out_rows, outer.est_rows)
        description = "" if on is None else f"on {render(on)}"
        candidates: list[Operator] = []

        if index_choice is not None:
            index, used_positions = index_choice
            leftover = [
                BoundBinary(
                    "=", equalities[i][0], shift_columns(equalities[i][1], width), DataType.BOOL
                )
                for i in range(len(equalities))
                if i not in used_positions
            ]
            shifted_local = [shift_columns(c, width) for c in local]
            all_residual = and_all(residual + leftover + shifted_local)
            index_join = IndexNestedLoopJoin(
                outer,
                relation.table,
                index,
                [compile_expr(equalities[i][0]) for i in used_positions],
                None if all_residual is None else compile_expr(all_residual),
                kind,
                description,
            )
            rows_t, _ = table_size(relation.table)
            used_eq = and_all(
                [
                    BoundBinary(
                        "=", equalities[i][0], shift_columns(equalities[i][1], width), DataType.BOOL
                    )
                    for i in used_positions
                ]
            )
            matches = rows_t * (1.0 if used_eq is None else selectivity(used_eq, lookup))
            if unique_inner:
                matches = min(matches, 1.0)
            index_join.est_cost = outer.est_cost + outer.est_rows * (index.tree.height() + matches)
            candidates.append(index_join)
        if equalities:
            residual_expr = and_all(residual)
            build_outer = kind == "INNER" and outer.est_rows < inner.est_rows
            hash_join = HashJoin(
                outer,
                inner,
                [compile_expr(o) for o, _ in equalities],
                [compile_expr(i) for _, i in equalities],
                None if residual_expr is None else compile_expr(residual_expr),
                kind,
                description,
                build_outer=build_outer,
            )
            hash_join.est_cost = (
                outer.est_cost + inner.est_cost + (outer.est_rows + inner.est_rows) * HASH_ROW
            )
            candidates.append(hash_join)
        loop = NestedLoopJoin(
            outer, inner, None if on is None else compile_expr(on), kind, description
        )
        loop.est_cost = outer.est_cost + outer.est_rows * inner.est_cost
        candidates.append(loop)

        for candidate in candidates:
            candidate.est_rows = out_rows
        return _choose(candidates, self.settings.join_strategy)


# --------------------------------------------------------------------- helpers


def _identity(index: int) -> int:
    return index


def _remapper(relations: Sequence[Relation], placed: Sequence[Relation]) -> Callable[[int], int]:
    """Maps a written-row position to its position in a row of ``placed`` in that order."""
    new_start: dict[int, int] = {}
    position = 0
    for relation in placed:
        new_start[relation.number] = position
        position += relation.width

    def remap(index: int) -> int:
        for relation in relations:
            if (
                relation.offset <= index < relation.offset + relation.width
                and relation.number in new_start
            ):
                return new_start[relation.number] + index - relation.offset
        raise ValueError(f"column {index} is not in the joined row")

    return remap


def _with_estimate(op: Operator, child: Operator) -> Operator:
    op.est_rows = child.est_rows
    op.est_cost = child.est_cost + child.est_rows * CPU_ROW
    return op


def _choose(candidates: list[Operator], strategy: str) -> Operator:
    if strategy == "auto":
        return min(candidates, key=lambda op: op.est_cost)
    preference = {
        "index_nested_loop": (IndexNestedLoopJoin, HashJoin, NestedLoopJoin),
        "hash": (HashJoin, NestedLoopJoin),
        "nested_loop": (NestedLoopJoin,),
    }[strategy]
    for kind in preference:
        for candidate in candidates:
            if isinstance(candidate, kind):
                return candidate
    return candidates[-1]


def _group_estimate(group_by: list[BoundExpr], rows: float, lookup: ColumnLookup) -> float:
    if not group_by:
        return 1.0
    groups = 1.0
    for expr in group_by:
        stats = lookup(expr.index)[0] if isinstance(expr, BoundColumn) else None
        groups *= (stats.distinct + (1 if stats.nulls else 0)) if stats else max(rows / 10, 1)
    return max(1.0, min(rows, groups))


def _split_equality(condition: BoundExpr, width: int) -> tuple[BoundExpr, BoundExpr] | None:
    """``outer_expr = inner_expr`` (either way round) as ``(outer, inner over the inner row)``."""
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


def _usable_index(
    table: TableInfo, equalities: list[tuple[BoundExpr, BoundExpr]]
) -> tuple[IndexInfo, list[int]] | None:
    """The index whose leading columns are matched by the most ``inner_column = outer``."""
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


# --------------------------------------------------------------------- index candidates


@dataclass(slots=True)
class _IndexCandidate:
    index: IndexInfo
    ranges: list[KeyRange]
    used: list[BoundExpr]
    selectivity: float


_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


def _compare_with_constant(expr: BoundExpr, column: int) -> tuple[str, Value] | None:
    """``column op constant`` (either way round), as ``(op, constant)``."""
    if not isinstance(expr, BoundBinary) or expr.op not in _FLIP:
        return None
    left, right = expr.left, expr.right
    if isinstance(left, BoundColumn) and left.index == column and isinstance(right, BoundLiteral):
        return expr.op, right.value
    if isinstance(right, BoundColumn) and right.index == column and isinstance(left, BoundLiteral):
        return _FLIP[expr.op], left.value
    return None


def _key_value(data_type: DataType, value: Value) -> tuple[bool, Value]:
    """Convert a constant to the index's key type; ``(False, None)`` if it cannot be."""
    if value is None:
        return False, None
    if data_type is DataType.INT and isinstance(value, int) and not isinstance(value, bool):
        return True, value
    if (
        data_type is DataType.FLOAT
        and isinstance(value, int | float)
        and not isinstance(value, bool)
    ):
        return True, float(value)
    if data_type is DataType.BOOL and isinstance(value, bool):
        return True, value
    if data_type is DataType.TEXT and isinstance(value, str):
        return len(value.encode("utf-8", "replace")) <= MAX_KEY_TEXT_BYTES, value
    return False, None


def _index_candidate(
    table: TableInfo, index: IndexInfo, conditions: list[BoundExpr], lookup: ColumnLookup
) -> _IndexCandidate | None:
    prefix: list[Value] = []
    used: list[BoundExpr] = []
    for column in index.columns:
        column_type = table.columns[column].type
        found = None
        for condition in conditions:
            match = _compare_with_constant(condition, column)
            if match is not None and match[0] == "=":
                ok, value = _key_value(column_type, match[1])
                if ok:
                    found = (condition, value)
                    break
        if found is None:
            break
        used.append(found[0])
        prefix.append(found[1])

    ranges: list[KeyRange] = []
    if len(prefix) < len(index.columns):
        column = index.columns[len(prefix)]
        column_type = table.columns[column].type
        low: Value = None
        high: Value = None
        low_inclusive = high_inclusive = True
        range_used: list[BoundExpr] = []
        for condition in conditions:
            if condition in used:
                continue
            bounds: list[tuple[str, Value]] = []
            match = _compare_with_constant(condition, column)
            if match is not None and match[0] != "=":
                bounds = [match]
            elif (
                isinstance(condition, BoundBetween)
                and not condition.negated
                and isinstance(condition.operand, BoundColumn)
                and condition.operand.index == column
                and isinstance(condition.low, BoundLiteral)
                and isinstance(condition.high, BoundLiteral)
            ):
                bounds = [(">=", condition.low.value), ("<=", condition.high.value)]
            converted = [(op, _key_value(column_type, v)) for op, v in bounds]
            if not converted or not all(ok for _, (ok, _) in converted):
                continue
            for op, (_, value) in converted:
                if op in (">", ">="):
                    inclusive = op == ">="
                    if low is None or value > low or (value == low and not inclusive):  # type: ignore[operator]
                        low, low_inclusive = value, inclusive
                else:
                    inclusive = op == "<="
                    if high is None or value < high or (value == high and not inclusive):  # type: ignore[operator]
                        high, high_inclusive = value, inclusive
            range_used.append(condition)
        if range_used:
            base = tuple(prefix)
            ranges = [
                KeyRange(
                    (*base, low) if low is not None else (base or None),
                    (*base, high) if high is not None else (base or None),
                    low_inclusive if low is not None else True,
                    high_inclusive if high is not None else True,
                )
            ]
            used += range_used
        elif not prefix:
            for condition in conditions:
                if (
                    isinstance(condition, BoundInList)
                    and not condition.negated
                    and isinstance(condition.operand, BoundColumn)
                    and condition.operand.index == column
                    and all(isinstance(i, BoundLiteral) for i in condition.items)
                ):
                    values = [_key_value(column_type, i.value) for i in condition.items]  # type: ignore[union-attr]
                    if all(ok for ok, _ in values):
                        distinct = sorted({v for _, v in values})  # type: ignore[type-var]
                        ranges = [KeyRange.point((v,)) for v in distinct]
                        used.append(condition)
                        break
    if not used:
        return None
    if not ranges:
        ranges = [KeyRange.point(tuple(prefix))]
    combined = and_all(used)
    assert combined is not None
    estimate = selectivity(combined, lookup)
    if index.unique and len(prefix) == len(index.columns):
        rows, _ = table_size(table)
        estimate = min(estimate, 1.0 / max(rows, 1.0))  # a full unique key matches one row
    return _IndexCandidate(index, ranges, used, estimate)
