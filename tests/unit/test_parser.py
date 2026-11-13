import pytest

from pydb.errors import SqlSyntaxError
from pydb.sql import ast
from pydb.sql.ast import (
    Between,
    BinaryOp,
    ColumnRef,
    FunctionCall,
    InList,
    IsNull,
    Like,
    Literal,
    UnaryOp,
)
from pydb.sql.parser import parse, parse_expression, parse_script
from pydb.types import DataType

a, b, c = ColumnRef("a"), ColumnRef("b"), ColumnRef("c")


def lit(v: ast.LiteralValue) -> Literal:
    return Literal(v)


# ------------------------------------------------------------------ expression precedence


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("a + b * c", BinaryOp("+", a, BinaryOp("*", b, c))),
        ("a * b + c", BinaryOp("+", BinaryOp("*", a, b), c)),
        ("a - b - c", BinaryOp("-", BinaryOp("-", a, b), c)),  # left-associative
        ("(a - b) * c", BinaryOp("*", BinaryOp("-", a, b), c)),
        ("a OR b AND c", BinaryOp("OR", a, BinaryOp("AND", b, c))),
        ("NOT a = b", UnaryOp("NOT", BinaryOp("=", a, b))),
        ("NOT a AND b", BinaryOp("AND", UnaryOp("NOT", a), b)),
        ("a = b AND b < c", BinaryOp("AND", BinaryOp("=", a, b), BinaryOp("<", b, c))),
        ("a || b = c", BinaryOp("=", BinaryOp("||", a, b), c)),
        ("a + b || c", BinaryOp("||", BinaryOp("+", a, b), c)),
        ("-a * b", BinaryOp("*", UnaryOp("-", a), b)),
        ("a % b / c", BinaryOp("/", BinaryOp("%", a, b), c)),
        ("a <> b", BinaryOp("!=", a, b)),
    ],
)
def test_precedence(sql: str, expected: ast.Expr) -> None:
    assert parse_expression(sql) == expected


def test_negative_literals_are_folded() -> None:
    assert parse_expression("-5") == lit(-5)
    assert parse_expression("-9223372036854775808") == lit(-(2**63))
    assert parse_expression("a - -2.5") == BinaryOp("-", a, lit(-2.5))
    assert parse_expression("-(5)") == UnaryOp("-", lit(5))
    assert parse_expression("+a") == a


def test_literals() -> None:
    assert parse_expression("NULL") == lit(None)
    assert parse_expression("TRUE") == lit(True)
    assert parse_expression("false") == lit(False)
    assert parse_expression("'x'") == lit("x")
    assert parse_expression("1.5") == lit(1.5)


def test_predicates() -> None:
    assert parse_expression("a IS NULL") == IsNull(a)
    assert parse_expression("a IS NOT NULL") == IsNull(a, negated=True)
    assert parse_expression("a IN (1, 2)") == InList(a, (lit(1), lit(2)))
    assert parse_expression("a NOT IN (b)") == InList(a, (b,), negated=True)
    assert parse_expression("a BETWEEN 1 AND 5 AND b") == BinaryOp(
        "AND", Between(a, lit(1), lit(5)), b
    )
    assert parse_expression("a NOT BETWEEN b + 1 AND c") == Between(
        a, BinaryOp("+", b, lit(1)), c, negated=True
    )
    assert parse_expression("a LIKE 'x%'") == Like(a, lit("x%"))
    assert parse_expression("a NOT LIKE b || 'z'") == Like(
        a, BinaryOp("||", b, lit("z")), negated=True
    )


def test_columns_and_functions() -> None:
    assert parse_expression("t.a") == ColumnRef("a", table="t")
    assert parse_expression('"T"."My Col"') == ColumnRef("My Col", table="T")
    assert parse_expression("count(*)") == FunctionCall("COUNT", star=True)
    assert parse_expression("Coalesce(a, 0)") == FunctionCall("COALESCE", (a, lit(0)))
    assert parse_expression("lower(upper(a))") == FunctionCall(
        "LOWER", (FunctionCall("UPPER", (a,)),)
    )


# ------------------------------------------------------------------ statements


def test_full_select() -> None:
    stmt = parse(
        "SELECT DISTINCT u.name AS n, COUNT(*) total, o.* FROM users u "
        "INNER JOIN orders AS o ON u.id = o.user_id LEFT OUTER JOIN x ON TRUE "
        "WHERE o.amount > 10 GROUP BY u.name HAVING COUNT(*) >= 2 "
        "ORDER BY total DESC, n LIMIT 10 OFFSET 5;"
    )
    assert stmt == ast.Select(
        items=(
            ast.SelectItem(ColumnRef("name", "u"), "n"),
            ast.SelectItem(FunctionCall("COUNT", star=True), "total"),
            ast.SelectItem(ast.Star("o")),
        ),
        from_table=ast.TableRef("users", "u"),
        joins=(
            ast.Join(
                "INNER",
                ast.TableRef("orders", "o"),
                BinaryOp("=", ColumnRef("id", "u"), ColumnRef("user_id", "o")),
            ),
            ast.Join("LEFT", ast.TableRef("x"), lit(True)),
        ),
        where=BinaryOp(">", ColumnRef("amount", "o"), lit(10)),
        group_by=(ColumnRef("name", "u"),),
        having=BinaryOp(">=", FunctionCall("COUNT", star=True), lit(2)),
        order_by=(ast.OrderItem(ColumnRef("total"), True), ast.OrderItem(ColumnRef("n"))),
        limit=10,
        offset=5,
        distinct=True,
    )


def test_select_without_from() -> None:
    assert parse("SELECT 1 + 1") == ast.Select((ast.SelectItem(BinaryOp("+", lit(1), lit(1))),))


def test_create_table() -> None:
    stmt = parse(
        "CREATE TABLE IF NOT EXISTS users ("
        " id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL UNIQUE,"
        " score REAL DEFAULT -1.5, active BOOLEAN DEFAULT TRUE, bio TEXT,"
        " key INT)"
    )
    assert stmt == ast.CreateTable(
        "users",
        (
            ast.ColumnDef("id", ast.TypeName(DataType.INT), primary_key=True),
            ast.ColumnDef("name", ast.TypeName(DataType.TEXT, 50), not_null=True, unique=True),
            ast.ColumnDef("score", ast.TypeName(DataType.FLOAT), default=lit(-1.5)),
            ast.ColumnDef("active", ast.TypeName(DataType.BOOL), default=lit(True)),
            ast.ColumnDef("bio", ast.TypeName(DataType.TEXT)),
            ast.ColumnDef("key", ast.TypeName(DataType.INT)),
        ),
        if_not_exists=True,
    )


def test_create_table_with_table_level_primary_key() -> None:
    stmt = parse("create table t (a int, b text, primary key (a, b))")
    assert isinstance(stmt, ast.CreateTable)
    assert stmt.primary_key == ("a", "b")


def test_tables_named_like_contextual_words() -> None:
    assert parse("CREATE TABLE if (a INT)") == ast.CreateTable(
        "if", (ast.ColumnDef("a", ast.TypeName(DataType.INT)),)
    )
    assert parse("DROP TABLE if") == ast.DropTable("if")
    assert parse("DROP TABLE IF EXISTS if") == ast.DropTable("if", if_exists=True)


def test_other_statements() -> None:
    assert parse("DROP TABLE t") == ast.DropTable("t")
    assert parse("CREATE UNIQUE INDEX idx ON t (a, b)") == ast.CreateIndex(
        "idx", "t", ("a", "b"), unique=True
    )
    assert parse("DROP INDEX IF EXISTS idx") == ast.DropIndex("idx", if_exists=True)
    assert parse("INSERT INTO t (a, b) VALUES (1, 'x'), (-2, NULL)") == ast.Insert(
        "t", ((lit(1), lit("x")), (lit(-2), lit(None))), ("a", "b")
    )
    assert parse("INSERT INTO t VALUES (1)") == ast.Insert("t", ((lit(1),),))
    assert parse("UPDATE t SET a = a + 1, b = 'y' WHERE a < 3") == ast.Update(
        "t",
        (("a", BinaryOp("+", a, lit(1))), ("b", lit("y"))),
        BinaryOp("<", a, lit(3)),
    )
    assert parse("DELETE FROM t") == ast.Delete("t")
    assert parse("begin") == ast.Begin()
    assert parse("COMMIT;") == ast.Commit()
    assert parse("ROLLBACK") == ast.Rollback()
    assert parse("SHOW TABLES") == ast.ShowTables()
    assert parse("DESCRIBE users") == ast.Describe("users")
    assert parse("ANALYZE users") == ast.Analyze("users")
    explain = parse("EXPLAIN ANALYZE SELECT a FROM t")
    assert isinstance(explain, ast.Explain) and explain.analyze


def test_parse_script() -> None:
    statements = parse_script("BEGIN; INSERT INTO t VALUES (1);; COMMIT; -- done\n")
    assert statements == [ast.Begin(), ast.Insert("t", ((lit(1),),)), ast.Commit()]
    assert parse_script("  -- nothing\n") == []


# ------------------------------------------------------------------ errors


@pytest.mark.parametrize(
    ("sql", "message", "column"),
    [
        ("SELECT a FORM t", "expected FROM, got 'FORM'", 10),
        ("SELECT * FORM users", "expected FROM, got 'FORM'", 10),
        ("SELECT * FROM t WHER a = 1", "expected WHERE, JOIN, GROUP BY", 17),
        ("SELECT a, FROM t", "expected an expression, got 'FROM'", 11),
        ("SELECT * FROM", "expected table name, got end of input", 14),
        ("SELEC a", "expected a statement, got 'SELEC'", 1),
        ("INSERT INTO t (a VALUES (1)", "expected ')', got 'VALUES'", 18),
        ("CREATE TABLE t (a BLOB)", "expected a type", 19),
        ("CREATE TABLE t (a INT NOT NULL NOT NULL)", "NOT NULL given twice", 32),
        ("CREATE TABLE t (a INT DEFAULT b)", "expected a literal after DEFAULT", 31),
        ("SELECT SUM(*) FROM t", "only allowed in COUNT", 12),
        ("SELECT a FROM t LIMIT -1", "expected LIMIT count", 23),
        ("UPDATE t SET a 1", "expected '='", 16),
        ("EXPLAIN DELETE FROM t", "expected SELECT after EXPLAIN", 9),
        ("SELECT a FROM t; SELECT b", "expected end of statement", 18),
        ("SELECT (a + b", "expected ')', got end of input", 14),
        ("CREATE TABLE t (a VARCHAR(0))", "positive VARCHAR length", 27),
    ],
)
def test_syntax_errors_name_the_problem_and_position(sql: str, message: str, column: int) -> None:
    with pytest.raises(SqlSyntaxError) as info:
        parse(sql)
    assert message in str(info.value)
    assert (info.value.line, info.value.column) == (1, column)


def test_brief_example_message() -> None:
    with pytest.raises(SqlSyntaxError) as info:
        parse("SELECT name, age FORM users")
    assert str(info.value) == "SyntaxError at line 1, col 18: expected FROM, got 'FORM'"


def test_error_on_a_later_line() -> None:
    with pytest.raises(SqlSyntaxError) as info:
        parse("SELECT a\nFROM t\nWHERE a = = 1")
    assert str(info.value) == "SyntaxError at line 3, col 11: expected an expression, got '='"


def test_script_requires_semicolons_between_statements() -> None:
    with pytest.raises(SqlSyntaxError, match="';' between statements"):
        parse_script("BEGIN COMMIT")


# ------------------------------------------------------------------ to_sql


def test_to_sql_quotes_what_needs_quoting() -> None:
    assert ColumnRef("My Col", "t").to_sql() == 't."My Col"'
    assert ColumnRef("select").to_sql() == '"select"'
    assert ColumnRef("key").to_sql() == "key"
    assert Literal("it's").to_sql() == "'it''s'"
    assert BinaryOp("-", a, lit(-1)).to_sql() == "(a - -1)"  # never "--", a comment


def test_to_sql_of_statements_reparses() -> None:
    sql = (
        "SELECT a, COUNT(*) AS n FROM t AS x LEFT JOIN u ON x.id = u.id "
        "WHERE a IN (1, 2) AND b IS NOT NULL GROUP BY a HAVING COUNT(*) > 1 "
        "ORDER BY n DESC LIMIT 3 OFFSET 1"
    )
    stmt = parse(sql)
    assert parse(stmt.to_sql()) == stmt
