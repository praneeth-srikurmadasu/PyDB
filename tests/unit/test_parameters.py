import pytest

from pydb.errors import ParameterError, SqlSyntaxError
from pydb.sql import ast
from pydb.sql.lexer import tokenize
from pydb.sql.parser import parse, parse_script
from pydb.sql.tokens import TokenType


def test_question_mark_is_a_token() -> None:
    assert [t.type for t in tokenize("a = ?")][:3] == [
        TokenType.IDENT,
        TokenType.OPERATOR,
        TokenType.PARAM,
    ]


def test_parameters_become_literals_in_order() -> None:
    statement = parse("INSERT INTO t VALUES (?, ?, ?, ?, ?)", [1, 2.5, "x", True, None])
    assert statement == ast.Insert(
        "t",
        (
            (
                ast.Literal(1),
                ast.Literal(2.5),
                ast.Literal("x"),
                ast.Literal(True),
                ast.Literal(None),
            ),
        ),
    )


def test_parameter_values_are_never_parsed_as_sql() -> None:
    hostile = "x'); DROP TABLE t; --"
    statement = parse("SELECT * FROM t WHERE name = ?", [hostile])
    assert isinstance(statement, ast.Select)
    assert statement.where == ast.BinaryOp("=", ast.ColumnRef("name"), ast.Literal(hostile))


def test_parameters_in_limit_and_offset() -> None:
    statement = parse("SELECT * FROM t LIMIT ? OFFSET ?", [5, 10])
    assert isinstance(statement, ast.Select)
    assert (statement.limit, statement.offset) == (5, 10)


@pytest.mark.parametrize("value", [-1, 1.5, "3", True])
def test_limit_parameter_must_be_a_non_negative_integer(value: object) -> None:
    with pytest.raises(ParameterError, match="LIMIT"):
        parse("SELECT * FROM t LIMIT ?", [value])


def test_negated_parameter() -> None:
    statement = parse("SELECT -?", [4])
    assert isinstance(statement, ast.Select)
    assert statement.items[0].expr == ast.UnaryOp("-", ast.Literal(4))


@pytest.mark.parametrize(
    ("sql", "params", "message"),
    [
        ("SELECT ?", None, "no parameters were given"),
        ("SELECT ?, ?", [1], "more '?' placeholders"),
        ("SELECT ?", [1, 2], "2 parameter(s) given"),
        ("SELECT 1", [1], "1 parameter(s) given"),
        ("SELECT ?", [b"bytes"], "unsupported type bytes"),
        ("SELECT ?", [[1]], "unsupported type list"),
    ],
)
def test_parameter_errors(sql: str, params: list[object] | None, message: str) -> None:
    with pytest.raises(ParameterError, match=__import__("re").escape(message)):
        parse(sql, params)


def test_empty_parameter_list_is_fine_without_placeholders() -> None:
    assert parse("SELECT 1", []) == parse("SELECT 1")


def test_scripts_do_not_take_parameters() -> None:
    with pytest.raises(ParameterError):
        parse_script("SELECT ?;")


def test_question_mark_in_a_string_is_text() -> None:
    statement = parse("SELECT '?'", [])
    assert isinstance(statement, ast.Select)
    assert statement.items[0].expr == ast.Literal("?")


def test_parameter_cannot_be_an_identifier() -> None:
    with pytest.raises(SqlSyntaxError):
        parse("SELECT * FROM ?", ["t"])
