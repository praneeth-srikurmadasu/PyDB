"""Bound expressions and their evaluation.

The binder turns AST expressions into the ``Bound*`` nodes below: column references become
positions in the input row and every node carries its result type (``None`` for an untyped
NULL literal). ``compile_expr`` then turns a bound tree into a Python closure once per query,
so evaluating a row is a chain of function calls with no type dispatch.

NULL follows SQL's three-valued logic. A comparison or arithmetic with NULL is NULL;
``AND`` is FALSE if either side is FALSE, otherwise NULL if either is NULL; ``OR`` is TRUE if
either side is TRUE, otherwise NULL if either is NULL; ``NOT NULL`` is NULL. A WHERE clause
keeps a row only when its predicate is TRUE.
"""

import math
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from pydb.errors import DivisionByZeroError, ValueTooLargeError
from pydb.storage.tuple import Row
from pydb.types import INT_MAX, INT_MIN, DataType, Value

Evaluator = Callable[[Row], Value]


@dataclass(frozen=True, slots=True)
class BoundLiteral:
    value: Value
    type: DataType | None


@dataclass(frozen=True, slots=True)
class BoundColumn:
    index: int
    type: DataType | None
    name: str


@dataclass(frozen=True, slots=True)
class BoundUnary:
    op: str  # "-" or "NOT"
    operand: "BoundExpr"
    type: DataType | None


@dataclass(frozen=True, slots=True)
class BoundBinary:
    op: str
    left: "BoundExpr"
    right: "BoundExpr"
    type: DataType | None


@dataclass(frozen=True, slots=True)
class BoundIsNull:
    operand: "BoundExpr"
    negated: bool
    type: DataType = DataType.BOOL


@dataclass(frozen=True, slots=True)
class BoundInList:
    operand: "BoundExpr"
    items: tuple["BoundExpr", ...]
    negated: bool
    type: DataType = DataType.BOOL


@dataclass(frozen=True, slots=True)
class BoundBetween:
    operand: "BoundExpr"
    low: "BoundExpr"
    high: "BoundExpr"
    negated: bool
    type: DataType = DataType.BOOL


@dataclass(frozen=True, slots=True)
class BoundLike:
    operand: "BoundExpr"
    pattern: "BoundExpr"
    negated: bool
    type: DataType = DataType.BOOL


@dataclass(frozen=True, slots=True)
class BoundFunction:
    name: str
    args: tuple["BoundExpr", ...]
    type: DataType | None


BoundExpr = (
    BoundLiteral
    | BoundColumn
    | BoundUnary
    | BoundBinary
    | BoundIsNull
    | BoundInList
    | BoundBetween
    | BoundLike
    | BoundFunction
)


def children(expr: "BoundExpr") -> tuple["BoundExpr", ...]:
    match expr:
        case BoundUnary(operand=operand) | BoundIsNull(operand=operand):
            return (operand,)
        case BoundBinary(left=left, right=right):
            return (left, right)
        case BoundInList(operand=operand, items=items):
            return (operand, *items)
        case BoundBetween(operand=operand, low=low, high=high):
            return (operand, low, high)
        case BoundLike(operand=operand, pattern=pattern):
            return (operand, pattern)
        case BoundFunction(args=args):
            return args
    return ()


def column_refs(expr: "BoundExpr") -> set[int]:
    """Positions of every input column the expression reads."""
    if isinstance(expr, BoundColumn):
        return {expr.index}
    refs: set[int] = set()
    for child in children(expr):
        refs |= column_refs(child)
    return refs


def shift_columns(expr: "BoundExpr", offset: int) -> "BoundExpr":
    """The same expression reading column ``i + offset`` wherever it read column ``i``."""
    match expr:
        case BoundColumn(index=index):
            return replace(expr, index=index + offset)
        case BoundUnary(operand=operand) | BoundIsNull(operand=operand):
            return replace(expr, operand=shift_columns(operand, offset))
        case BoundBinary(left=left, right=right):
            return replace(
                expr, left=shift_columns(left, offset), right=shift_columns(right, offset)
            )
        case BoundInList(operand=operand, items=items):
            return replace(
                expr,
                operand=shift_columns(operand, offset),
                items=tuple(shift_columns(i, offset) for i in items),
            )
        case BoundBetween(operand=operand, low=low, high=high):
            return replace(
                expr,
                operand=shift_columns(operand, offset),
                low=shift_columns(low, offset),
                high=shift_columns(high, offset),
            )
        case BoundLike(operand=operand, pattern=pattern):
            return replace(
                expr, operand=shift_columns(operand, offset), pattern=shift_columns(pattern, offset)
            )
        case BoundFunction(args=args):
            return replace(expr, args=tuple(shift_columns(a, offset) for a in args))
    return expr


def conjuncts(expr: "BoundExpr") -> list["BoundExpr"]:
    """Split ``a AND b AND c`` into ``[a, b, c]``."""
    if isinstance(expr, BoundBinary) and expr.op == "AND":
        return conjuncts(expr.left) + conjuncts(expr.right)
    return [expr]


def and_all(exprs: list["BoundExpr"]) -> "BoundExpr | None":
    if not exprs:
        return None
    result = exprs[0]
    for expr in exprs[1:]:
        result = BoundBinary("AND", result, expr, DataType.BOOL)
    return result


# --------------------------------------------------------------------- three-valued logic


def sql_and(left: Value, right: Value) -> Value:
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def sql_or(left: Value, right: Value) -> Value:
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def sql_not(value: Value) -> Value:
    return None if value is None else not value


def like_to_regex(pattern: str) -> re.Pattern[str]:
    """``%`` matches any run of characters, ``_`` exactly one; everything else is literal."""
    parts = [".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern]
    return re.compile("".join(parts), re.DOTALL)


# --------------------------------------------------------------------- compilation

_COMPARE: dict[str, Callable[[Any, Any], Any]] = {
    "=": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def compile_expr(expr: BoundExpr) -> Evaluator:
    match expr:
        case BoundLiteral(value=value):
            return lambda row: value
        case BoundColumn(index=index):
            return lambda row: row[index]
        case BoundUnary(op="NOT", operand=operand):
            inner = compile_expr(operand)
            return lambda row: sql_not(inner(row))
        case BoundUnary(op="-", operand=operand, type=result_type):
            inner = compile_expr(operand)

            def negate(row: Row) -> Value:
                value = inner(row)
                if value is None:
                    return None
                assert isinstance(value, int | float)
                return _check_int(-int(value)) if result_type is DataType.INT else -value

            return negate
        case BoundBinary(op=op, left=left, right=right, type=result_type):
            return _compile_binary(op, compile_expr(left), compile_expr(right), result_type)
        case BoundIsNull(operand=operand, negated=negated):
            inner = compile_expr(operand)
            if negated:
                return lambda row: inner(row) is not None
            return lambda row: inner(row) is None
        case BoundInList(operand=operand, items=items, negated=negated):
            return _compile_in(compile_expr(operand), [compile_expr(i) for i in items], negated)
        case BoundBetween(operand=operand, low=low, high=high, negated=negated):
            value_of, low_of, high_of = compile_expr(operand), compile_expr(low), compile_expr(high)
            ge, le = _COMPARE[">="], _COMPARE["<="]

            def between(row: Row) -> Value:
                value = value_of(row)
                result = sql_and(
                    _null_or(ge, value, low_of(row)), _null_or(le, value, high_of(row))
                )
                return sql_not(result) if negated else result

            return between
        case BoundLike(operand=operand, pattern=pattern, negated=negated):
            return _compile_like(compile_expr(operand), pattern, negated)
        case BoundFunction(name=name, args=args):
            return _compile_function(name, [compile_expr(a) for a in args])
    raise TypeError(f"cannot compile {expr!r}")


def _null_or(op: Callable[[Any, Any], Any], left: Value, right: Value) -> Value:
    if left is None or right is None:
        return None
    return bool(op(left, right))


def _check_int(value: int) -> int:
    if not INT_MIN <= value <= INT_MAX:
        raise ValueTooLargeError(f"integer out of range: {value}")
    return value


def _int_divide(left: int, right: int) -> int:
    """Integer division truncating toward zero, as in SQL (Python's // floors)."""
    quotient = abs(left) // abs(right)
    return quotient if (left < 0) == (right < 0) else -quotient


def _compile_binary(
    op: str, left: Evaluator, right: Evaluator, result_type: DataType | None
) -> Evaluator:
    if op == "AND":
        return lambda row: sql_and(left(row), right(row))
    if op == "OR":
        return lambda row: sql_or(left(row), right(row))
    if op in _COMPARE:
        compare = _COMPARE[op]
        return lambda row: _null_or(compare, left(row), right(row))
    if op == "||":

        def concat(row: Row) -> Value:
            a, b = left(row), right(row)
            if a is None or b is None:
                return None
            return f"{a}{b}"

        return concat

    integer = result_type is DataType.INT

    def arithmetic(row: Row) -> Value:
        a, b = left(row), right(row)
        if a is None or b is None:
            return None
        assert isinstance(a, int | float) and isinstance(b, int | float)
        if op == "+":
            result = a + b
        elif op == "-":
            result = a - b
        elif op == "*":
            result = a * b
        else:
            if b == 0:
                raise DivisionByZeroError("division by zero")
            if op == "/":
                result = _int_divide(int(a), int(b)) if integer else a / b
            else:  # "%": the result takes the sign of the dividend
                result = (
                    int(a) - int(b) * _int_divide(int(a), int(b)) if integer else _float_mod(a, b)
                )
        return _check_int(int(result)) if integer else float(result)

    return arithmetic


def _float_mod(a: float, b: float) -> float:
    return math.fmod(a, b)  # sign of the dividend, like the integer case


def _abs(value: Value) -> Value:
    if isinstance(value, int) and not isinstance(value, bool):
        return _check_int(abs(value))
    if isinstance(value, float):
        return abs(value)
    raise TypeError(f"ABS of {value!r}")


def _compile_in(value_of: Evaluator, items: list[Evaluator], negated: bool) -> Evaluator:
    def in_list(row: Row) -> Value:
        value = value_of(row)
        if value is None:
            return None
        saw_null = False
        for item in items:
            candidate = item(row)
            if candidate is None:
                saw_null = True
            elif candidate == value:
                return not negated
        if saw_null:
            return None
        return negated

    return in_list


def _compile_like(value_of: Evaluator, pattern: BoundExpr, negated: bool) -> Evaluator:
    if isinstance(pattern, BoundLiteral) and isinstance(pattern.value, str):
        regex = like_to_regex(pattern.value)

        def like_constant(row: Row) -> Value:
            value = value_of(row)
            if value is None:
                return None
            return (regex.fullmatch(str(value)) is None) == negated

        return like_constant
    pattern_of = compile_expr(pattern)

    def like(row: Row) -> Value:
        value, pat = value_of(row), pattern_of(row)
        if value is None or pat is None:
            return None
        return (like_to_regex(str(pat)).fullmatch(str(value)) is None) == negated

    return like


def _compile_function(name: str, args: list[Evaluator]) -> Evaluator:
    if name == "COALESCE":

        def coalesce(row: Row) -> Value:
            for arg in args:
                value = arg(row)
                if value is not None:
                    return value
            return None

        return coalesce
    (arg,) = args
    unary: dict[str, Callable[[Value], Value]] = {
        "LOWER": lambda v: str(v).lower(),
        "UPPER": lambda v: str(v).upper(),
        "LENGTH": lambda v: len(str(v)),
        "ABS": _abs,
    }
    fn = unary[name]

    def apply(row: Row) -> Value:
        value = arg(row)
        return None if value is None else fn(value)

    return apply
