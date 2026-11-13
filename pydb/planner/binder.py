"""Binding: resolving names against the catalog and type-checking expressions.

The binder takes AST nodes and produces bound statements in which every column reference
is a position in an input row and every expression has a known result type. All semantic
errors (unknown names, ambiguity, type mismatches) are raised here, before any data is read
or written.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydb.catalog.catalog import Catalog
from pydb.catalog.schema import TableInfo
from pydb.errors import (
    AmbiguousColumnError,
    CatalogError,
    NotSupportedError,
    TypeMismatchError,
    UnknownColumnError,
    UnknownFunctionError,
)
from pydb.execution.expressions import (
    BoundBetween,
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundFunction,
    BoundInList,
    BoundIsNull,
    BoundLike,
    BoundLiteral,
    BoundUnary,
)
from pydb.sql import ast
from pydb.types import DataType

NUMERIC = frozenset({DataType.INT, DataType.FLOAT})
AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})
SCALAR_FUNCTIONS = frozenset({"LOWER", "UPPER", "LENGTH", "ABS", "COALESCE"})


@dataclass(frozen=True, slots=True)
class ScopeColumn:
    table: str  # the table's alias, or its name when it has none
    name: str
    type: DataType


class Scope:
    """The columns visible to an expression, in input-row order."""

    def __init__(self, columns: Sequence[ScopeColumn] = ()) -> None:
        self.columns = list(columns)

    @classmethod
    def for_table(cls, table: TableInfo, alias: str | None = None) -> "Scope":
        label = alias or table.name
        return cls([ScopeColumn(label, c.name, c.type) for c in table.columns])

    def resolve(self, ref: ast.ColumnRef) -> BoundColumn:
        matches = [
            i
            for i, col in enumerate(self.columns)
            if col.name == ref.name and (ref.table is None or col.table == ref.table)
        ]
        if not matches:
            if ref.table is not None and all(c.table != ref.table for c in self.columns):
                raise UnknownColumnError(
                    f'missing table "{ref.table}" for column reference {ref.to_sql()}'
                )
            available = ", ".join(f"{c.table}.{c.name}" for c in self.columns) or "none"
            raise UnknownColumnError(
                f'column "{ref.to_sql()}" does not exist (available columns: {available})'
            )
        if len(matches) > 1:
            tables = ", ".join(self.columns[i].table for i in matches)
            raise AmbiguousColumnError(
                f'column reference "{ref.name}" is ambiguous (it exists in {tables}); '
                "qualify it with a table name"
            )
        index = matches[0]
        return BoundColumn(index, self.columns[index].type, self.columns[index].name)


# --------------------------------------------------------------------- bound statements


@dataclass(frozen=True, slots=True)
class BoundSelect:
    table: TableInfo | None
    where: BoundExpr | None
    outputs: tuple[tuple[str, BoundExpr], ...]


@dataclass(frozen=True, slots=True)
class BoundInsert:
    table: TableInfo
    positions: tuple[int, ...]  # target column for each value in a row
    rows: tuple[tuple[BoundExpr, ...], ...]


@dataclass(frozen=True, slots=True)
class BoundUpdate:
    table: TableInfo
    assignments: tuple[tuple[int, BoundExpr], ...]
    where: BoundExpr | None


@dataclass(frozen=True, slots=True)
class BoundDelete:
    table: TableInfo
    where: BoundExpr | None


class Binder:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    # ----------------------------------------------------------------- statements

    def bind_select(self, select: ast.Select) -> BoundSelect:
        for present, feature in (
            (select.joins, "JOIN"),
            (select.group_by, "GROUP BY"),
            (select.having, "HAVING"),
            (select.order_by, "ORDER BY"),
            (select.limit is not None, "LIMIT"),
            (select.distinct, "DISTINCT"),
        ):
            if present:
                raise NotSupportedError(f"{feature} is not supported yet")
        table: TableInfo | None = None
        scope = Scope()
        if select.from_table is not None:
            table = self.catalog.get_table(select.from_table.name)
            scope = Scope.for_table(table, select.from_table.alias)
        outputs: list[tuple[str, BoundExpr]] = []
        for item in select.items:
            if isinstance(item.expr, ast.Star):
                outputs.extend(self._expand_star(item.expr, scope))
                continue
            bound = self.bind_expr(item.expr, scope)
            outputs.append((item.alias or _output_name(item.expr), bound))
        where = self._bind_predicate(select.where, scope, "WHERE")
        return BoundSelect(table, where, tuple(outputs))

    def bind_insert(self, insert: ast.Insert) -> BoundInsert:
        table = self._writable_table(insert.table)
        if insert.columns is None:
            positions = tuple(range(len(table.columns)))
        else:
            positions = tuple(self._column_position(table, name) for name in insert.columns)
            seen = [table.columns[p].name for p in positions]
            for name in seen:
                if seen.count(name) > 1:
                    raise CatalogError(f'column "{name}" specified more than once')
        rows: list[tuple[BoundExpr, ...]] = []
        for row in insert.rows:
            if len(row) != len(positions):
                raise TypeMismatchError(
                    f"INSERT has {len(row)} values but {len(positions)} target columns"
                )
            bound_row = tuple(self.bind_expr(e, Scope()) for e in row)
            for position, expr in zip(positions, bound_row, strict=True):
                _check_assignable(table.columns[position].name, table.columns[position].type, expr)
            rows.append(bound_row)
        return BoundInsert(table, positions, tuple(rows))

    def bind_update(self, update: ast.Update) -> BoundUpdate:
        table = self._writable_table(update.table)
        scope = Scope.for_table(table)
        assignments: list[tuple[int, BoundExpr]] = []
        for name, expr in update.assignments:
            position = self._column_position(table, name)
            if any(p == position for p, _ in assignments):
                raise CatalogError(f'column "{name}" assigned more than once')
            bound = self.bind_expr(expr, scope)
            _check_assignable(name, table.columns[position].type, bound)
            assignments.append((position, bound))
        where = self._bind_predicate(update.where, scope, "WHERE")
        return BoundUpdate(table, tuple(assignments), where)

    def bind_delete(self, delete: ast.Delete) -> BoundDelete:
        table = self._writable_table(delete.table)
        where = self._bind_predicate(delete.where, Scope.for_table(table), "WHERE")
        return BoundDelete(table, where)

    # ----------------------------------------------------------------- expressions

    def bind_expr(self, expr: ast.Expr, scope: Scope) -> BoundExpr:
        match expr:
            case ast.Literal(value=literal):
                return BoundLiteral(literal, _literal_type(literal))
            case ast.ColumnRef():
                return scope.resolve(expr)
            case ast.UnaryOp(op="NOT", operand=operand):
                inner = self.bind_expr(operand, scope)
                _require(inner, {DataType.BOOL}, "NOT")
                return BoundUnary("NOT", inner, DataType.BOOL)
            case ast.UnaryOp(op=op, operand=operand):
                inner = self.bind_expr(operand, scope)
                _require(inner, NUMERIC, f"unary {op}")
                return BoundUnary(op, inner, inner.type)
            case ast.BinaryOp(op=op, left=left, right=right):
                return self._bind_binary(
                    op, self.bind_expr(left, scope), self.bind_expr(right, scope)
                )
            case ast.IsNull(operand=operand, negated=negated):
                return BoundIsNull(self.bind_expr(operand, scope), negated)
            case ast.InList(operand=operand, items=items, negated=negated):
                value = self.bind_expr(operand, scope)
                bound_items = tuple(self.bind_expr(i, scope) for i in items)
                for item in bound_items:
                    _require_comparable(value, item, "IN")
                return BoundInList(value, bound_items, negated)
            case ast.Between(operand=operand, low=low, high=high, negated=negated):
                value = self.bind_expr(operand, scope)
                lo, hi = self.bind_expr(low, scope), self.bind_expr(high, scope)
                _require_comparable(value, lo, "BETWEEN")
                _require_comparable(value, hi, "BETWEEN")
                return BoundBetween(value, lo, hi, negated)
            case ast.Like(operand=operand, pattern=pattern, negated=negated):
                value, pat = self.bind_expr(operand, scope), self.bind_expr(pattern, scope)
                _require(value, {DataType.TEXT}, "LIKE")
                _require(pat, {DataType.TEXT}, "LIKE")
                return BoundLike(value, pat, negated)
            case ast.FunctionCall():
                return self._bind_function(expr, scope)
        raise TypeError(f"cannot bind {expr!r}")

    def _bind_binary(self, op: str, left: BoundExpr, right: BoundExpr) -> BoundExpr:
        if op in ("AND", "OR"):
            _require(left, {DataType.BOOL}, op)
            _require(right, {DataType.BOOL}, op)
            return BoundBinary(op, left, right, DataType.BOOL)
        if op in ("=", "!=", "<", "<=", ">", ">="):
            _require_comparable(left, right, op)
            return BoundBinary(op, left, right, DataType.BOOL)
        if op == "||":
            _require(left, {DataType.TEXT}, "||")
            _require(right, {DataType.TEXT}, "||")
            return BoundBinary(op, left, right, DataType.TEXT)
        _require(left, NUMERIC, op)
        _require(right, NUMERIC, op)
        if left.type is None and right.type is None:
            result: DataType | None = None
        elif DataType.FLOAT in (left.type, right.type):
            result = DataType.FLOAT
        else:
            result = DataType.INT
        return BoundBinary(op, left, right, result)

    def _bind_function(self, call: ast.FunctionCall, scope: Scope) -> BoundExpr:
        name = call.name
        if name in AGGREGATES:
            raise NotSupportedError(f"aggregate function {name} is not supported yet")
        if name not in SCALAR_FUNCTIONS:
            raise UnknownFunctionError(f"function {name} does not exist")
        args = tuple(self.bind_expr(a, scope) for a in call.args)
        if name == "COALESCE":
            if not args:
                raise TypeMismatchError("COALESCE needs at least one argument")
            for arg in args[1:]:
                _require_comparable(args[0], arg, "COALESCE")
            types = {a.type for a in args} - {None}
            result = DataType.FLOAT if types == set(NUMERIC) else next(iter(types), None)
            return BoundFunction(name, args, result)
        if len(args) != 1:
            raise TypeMismatchError(f"{name} takes exactly one argument, got {len(args)}")
        (arg,) = args
        if name in ("LOWER", "UPPER"):
            _require(arg, {DataType.TEXT}, name)
            return BoundFunction(name, args, DataType.TEXT)
        if name == "LENGTH":
            _require(arg, {DataType.TEXT}, name)
            return BoundFunction(name, args, DataType.INT)
        _require(arg, NUMERIC, name)  # ABS
        return BoundFunction(name, args, arg.type)

    # ----------------------------------------------------------------- helpers

    def _bind_predicate(self, expr: ast.Expr | None, scope: Scope, clause: str) -> BoundExpr | None:
        if expr is None:
            return None
        bound = self.bind_expr(expr, scope)
        _require(bound, {DataType.BOOL}, clause)
        return bound

    def _expand_star(self, star: ast.Star, scope: Scope) -> list[tuple[str, BoundExpr]]:
        if not scope.columns:
            raise UnknownColumnError("SELECT * needs a FROM clause")
        if star.table is not None and all(c.table != star.table for c in scope.columns):
            raise UnknownColumnError(f'missing table "{star.table}" for {star.to_sql()}')
        return [
            (col.name, BoundColumn(i, col.type, col.name))
            for i, col in enumerate(scope.columns)
            if star.table is None or col.table == star.table
        ]

    def _writable_table(self, name: str) -> TableInfo:
        table = self.catalog.get_table(name)
        if table.is_system:
            raise CatalogError(f'system table "{name}" is read-only')
        return table

    @staticmethod
    def _column_position(table: TableInfo, name: str) -> int:
        position = table.column_position(name)
        if position is None:
            raise UnknownColumnError(
                f'column "{name}" of table "{table.name}" does not exist '
                f"(columns: {', '.join(table.column_names())})"
            )
        return position


def _literal_type(value: object) -> DataType | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return DataType.BOOL
    if isinstance(value, int):
        return DataType.INT
    if isinstance(value, float):
        return DataType.FLOAT
    return DataType.TEXT


def _type_name(expr: BoundExpr) -> str:
    return "NULL" if expr.type is None else expr.type.name


def _require(expr: BoundExpr, allowed: set[DataType] | frozenset[DataType], context: str) -> None:
    if expr.type is not None and expr.type not in allowed:
        expected = " or ".join(sorted(t.name for t in allowed))
        raise TypeMismatchError(f"{context} expects {expected}, got {_type_name(expr)}")


def _require_comparable(left: BoundExpr, right: BoundExpr, context: str) -> None:
    a, b = left.type, right.type
    if a is None or b is None or a == b or {a, b} <= NUMERIC:
        return
    raise TypeMismatchError(f"cannot compare {a.name} with {b.name} in {context}")


def _check_assignable(column: str, target: DataType, expr: BoundExpr) -> None:
    source = expr.type
    if source is None or source == target or (source is DataType.INT and target is DataType.FLOAT):
        return
    raise TypeMismatchError(f'column "{column}" is {target.name} but the value is {source.name}')


def _output_name(expr: ast.Expr) -> str:
    if isinstance(expr, ast.ColumnRef):
        return expr.name
    if isinstance(expr, ast.FunctionCall):
        return expr.name.lower()
    return expr.to_sql()
