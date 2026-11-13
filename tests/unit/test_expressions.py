import pytest

from pydb.errors import DivisionByZeroError, ValueTooLargeError
from pydb.execution.expressions import (
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundLiteral,
    compile_expr,
    like_to_regex,
)
from pydb.types import INT_MAX, DataType, Value

INT, FLOAT = DataType.INT, DataType.FLOAT


def lit(value: Value) -> BoundLiteral:
    if isinstance(value, bool):
        return BoundLiteral(value, DataType.BOOL)
    if isinstance(value, int):
        return BoundLiteral(value, INT)
    if isinstance(value, float):
        return BoundLiteral(value, FLOAT)
    return BoundLiteral(value, DataType.TEXT if isinstance(value, str) else None)


def evaluate(expr: BoundExpr, row: tuple[Value, ...] = ()) -> Value:
    return compile_expr(expr)(row)


def arith(op: str, a: Value, b: Value) -> Value:
    result_type = FLOAT if isinstance(a, float) or isinstance(b, float) else INT
    return evaluate(BoundBinary(op, lit(a), lit(b), result_type))


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("+", 2, 3, 5),
        ("-", 2, 3, -1),
        ("*", -4, 3, -12),
        ("/", 7, 2, 3),
        ("/", -7, 2, -3),  # truncates toward zero, unlike Python's //
        ("/", 7, -2, -3),
        ("%", -7, 2, -1),  # takes the sign of the dividend
        ("%", 7, -2, 1),
        ("/", 7.0, 2, 3.5),
        ("+", 1, 0.5, 1.5),
        ("%", -7.5, 2, -1.5),
    ],
)
def test_arithmetic(op: str, a: Value, b: Value, expected: Value) -> None:
    result = arith(op, a, b)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize("op", ["/", "%"])
def test_division_by_zero(op: str) -> None:
    with pytest.raises(DivisionByZeroError):
        arith(op, 1, 0)
    with pytest.raises(DivisionByZeroError):
        arith(op, 1.0, 0)


def test_integer_overflow_is_an_error() -> None:
    with pytest.raises(ValueTooLargeError, match="out of range"):
        arith("+", INT_MAX, 1)
    with pytest.raises(ValueTooLargeError):
        arith("*", INT_MAX, 2)


def test_columns_read_from_the_row() -> None:
    expr = BoundBinary("*", BoundColumn(1, INT, "b"), lit(10), INT)
    assert evaluate(expr, ("x", 4)) == 40


@pytest.mark.parametrize(
    ("pattern", "text", "matches"),
    [
        ("a%", "abc", True),
        ("a%", "ba", False),
        ("%b%", "abc", True),
        ("a_c", "abc", True),
        ("a_c", "abbc", False),
        ("100%", "100%", True),
        ("a.c", "abc", False),  # regex characters are literal
        ("%", "", True),
        ("A%", "abc", False),  # case-sensitive
        ("a%", "a\nb", True),
    ],
)
def test_like_patterns(pattern: str, text: str, matches: bool) -> None:
    assert (like_to_regex(pattern).fullmatch(text) is not None) == matches
