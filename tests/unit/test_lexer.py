import re

import pytest

from pydb.errors import SqlSyntaxError
from pydb.sql.lexer import tokenize
from pydb.sql.tokens import TokenType as T


def kinds(sql: str) -> list[tuple[T, object]]:
    return [(tok.type, tok.value) for tok in tokenize(sql)[:-1]]


def test_keywords_are_case_insensitive_and_identifiers_fold_to_lower() -> None:
    assert kinds("select Name FrOm Users") == [
        (T.KEYWORD, "SELECT"),
        (T.IDENT, "name"),
        (T.KEYWORD, "FROM"),
        (T.IDENT, "users"),
    ]


def test_quoted_identifiers_keep_case_and_unescape() -> None:
    assert kinds('"Mixed Case" "say ""hi"""') == [(T.IDENT, "Mixed Case"), (T.IDENT, 'say "hi"')]


def test_contextual_words_are_identifiers() -> None:
    assert kinds("key tables begin text") == [
        (T.IDENT, "key"),
        (T.IDENT, "tables"),
        (T.IDENT, "begin"),
        (T.IDENT, "text"),
    ]


def test_numbers() -> None:
    assert kinds("0 42 3.5 .5 2. 1e3 1.5E-2 7e+1") == [
        (T.INTEGER, 0),
        (T.INTEGER, 42),
        (T.FLOAT, 3.5),
        (T.FLOAT, 0.5),
        (T.FLOAT, 2.0),
        (T.FLOAT, 1000.0),
        (T.FLOAT, 0.015),
        (T.FLOAT, 70.0),
    ]


def test_big_integers_are_not_truncated() -> None:
    assert kinds("9223372036854775808") == [(T.INTEGER, 2**63)]


def test_strings_with_doubled_quotes_and_newlines() -> None:
    assert kinds("'it''s' '' 'a\nb'") == [(T.STRING, "it's"), (T.STRING, ""), (T.STRING, "a\nb")]


def test_operators_and_punctuation() -> None:
    assert [v for _, v in kinds("= != <> < <= > >= + - * / % || ( ) , . ;")] == [
        "=", "!=", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "%", "||",
        "(", ")", ",", ".", ";",
    ]  # fmt: skip


def test_comments_are_skipped() -> None:
    sql = "SELECT -- trailing comment\n a /* block\n comment */ FROM t"
    assert [v for _, v in kinds(sql)] == ["SELECT", "a", "FROM", "t"]


def test_line_and_column_are_tracked() -> None:
    tokens = tokenize("SELECT a,\n  bb FROM\n\tt")
    positions = [(tok.text, tok.line, tok.column) for tok in tokens]
    assert positions == [
        ("SELECT", 1, 1),
        ("a", 1, 8),
        (",", 1, 9),
        ("bb", 2, 3),
        ("FROM", 2, 6),
        ("t", 3, 2),
        ("", 3, 3),
    ]


@pytest.mark.parametrize(
    ("sql", "message", "line", "column"),
    [
        ("SELECT 'abc", "unterminated string", 1, 8),
        ('SELECT "abc', "unterminated identifier", 1, 8),
        ("SELECT /* never closed", "unterminated /* comment", 1, 8),
        ("SELECT a\n  FROM t WHERE x $ 1", "unexpected character '$'", 2, 18),
        ("SELECT 12abc", "invalid number", 1, 8),
        ('SELECT ""', "empty quoted identifier", 1, 8),
    ],
)
def test_lexical_errors_report_position(sql: str, message: str, line: int, column: int) -> None:
    with pytest.raises(SqlSyntaxError, match=re.escape(message)) as info:
        tokenize(sql)
    assert (info.value.line, info.value.column) == (line, column)
    assert str(info.value).startswith(f"SyntaxError at line {line}, col {column}: ")
