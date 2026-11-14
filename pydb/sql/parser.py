"""Recursive-descent parser for statements, with a Pratt parser for expressions.

Expression binding powers (higher binds tighter)::

    OR 1 < AND 2 < NOT 3 < comparison, IS, IN, BETWEEN, LIKE 4 < || 5 < + - 6 < * / % 7 < unary - 8

``parse_expression(min_bp)`` parses a prefix expression, then keeps absorbing infix operators
whose binding power is greater than ``min_bp``, parsing each right operand with that power as
its own minimum. Equal powers therefore associate to the left.

A unary minus written directly before a numeric literal is folded into the literal, so
``-9223372036854775808`` is one INT literal rather than the negation of an out-of-range one.
"""

from collections.abc import Callable, Sequence
from typing import NoReturn, TypeVar

from pydb.errors import ParameterError, SqlSyntaxError
from pydb.sql import ast
from pydb.sql.lexer import tokenize
from pydb.sql.tokens import Token, TokenType
from pydb.types import DataType

T = TypeVar("T")

_COMPARISON_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})
_BP_OR, _BP_AND, _BP_NOT, _BP_COMPARE, _BP_CONCAT, _BP_ADD, _BP_MUL, _BP_UNARY = range(1, 9)
_INFIX_POWER = {
    "OR": _BP_OR,
    "AND": _BP_AND,
    "||": _BP_CONCAT,
    "+": _BP_ADD,
    "-": _BP_ADD,
    "*": _BP_MUL,
    "/": _BP_MUL,
    "%": _BP_MUL,
    **dict.fromkeys(_COMPARISON_OPS, _BP_COMPARE),
    **dict.fromkeys(("IS", "IN", "BETWEEN", "LIKE", "NOT"), _BP_COMPARE),
}
_TYPE_NAMES = {
    "INT": DataType.INT,
    "INTEGER": DataType.INT,
    "FLOAT": DataType.FLOAT,
    "REAL": DataType.FLOAT,
    "TEXT": DataType.TEXT,
    "BOOL": DataType.BOOL,
    "BOOLEAN": DataType.BOOL,
}


def parse(sql: str, params: Sequence[object] | None = None) -> ast.Statement:
    """Parse exactly one statement, optionally followed by a semicolon.

    Each ``?`` in the statement takes the next value of ``params`` and becomes a literal in
    the tree, so values are never spliced into SQL text and cannot change its meaning.
    """
    parser = _Parser(tokenize(sql), params)
    statement = parser.statement()
    parser.accept(TokenType.SEMICOLON)
    parser.expect_end()
    parser.check_all_params_used()
    return statement


def parse_script(sql: str) -> list[ast.Statement]:
    """Parse zero or more statements separated by semicolons."""
    parser = _Parser(tokenize(sql))
    statements: list[ast.Statement] = []
    while True:
        while parser.accept(TokenType.SEMICOLON):
            pass
        if parser.at_end():
            return statements
        statements.append(parser.statement())
        if not parser.at_end():
            parser.expect(TokenType.SEMICOLON, "';' between statements")


def parse_expression(sql: str) -> ast.Expr:
    parser = _Parser(tokenize(sql))
    expr = parser.expression()
    parser.expect_end()
    return expr


class _Parser:
    def __init__(self, tokens: list[Token], params: Sequence[object] | None = None) -> None:
        self.tokens = tokens
        self.pos = 0
        self.params = params
        self.params_used = 0

    def parameter(self) -> ast.LiteralValue:
        """Consume a ``?`` token and return the parameter value it stands for."""
        self.advance()
        if self.params is None:
            raise ParameterError("the statement has a '?' placeholder but no parameters were given")
        if self.params_used >= len(self.params):
            raise ParameterError(
                f"the statement has more '?' placeholders than the {len(self.params)} "
                "parameter(s) given"
            )
        value = self.params[self.params_used]
        self.params_used += 1
        if value is not None and not isinstance(value, int | float | str | bool):
            raise ParameterError(
                f"parameter {self.params_used} has unsupported type {type(value).__name__}; "
                "use int, float, str, bool or None"
            )
        return value

    def check_all_params_used(self) -> None:
        if self.params is not None and self.params_used != len(self.params):
            raise ParameterError(
                f"{len(self.params)} parameter(s) given but the statement has "
                f"{self.params_used} '?' placeholder(s)"
            )

    # ----------------------------------------------------------------- token helpers

    @property
    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def at_identifier(self) -> bool:
        return self.current.type is TokenType.IDENT

    def at_end(self) -> bool:
        return self.current.type is TokenType.EOF

    def advance(self) -> Token:
        token = self.current
        if token.type is not TokenType.EOF:
            self.pos += 1
        return token

    def fail(self, expected: str, token: Token | None = None) -> NoReturn:
        token = self.current if token is None else token
        raise SqlSyntaxError(
            f"expected {expected}, got {token.describe()}", token.line, token.column
        )

    def accept(self, type_: TokenType) -> Token | None:
        if self.current.type is type_:
            return self.advance()
        return None

    def expect(self, type_: TokenType, what: str) -> Token:
        token = self.accept(type_)
        if token is None:
            self.fail(what)
        return token

    def is_keyword(self, *words: str, token: Token | None = None) -> bool:
        token = self.current if token is None else token
        return token.type is TokenType.KEYWORD and token.value in words

    def accept_keyword(self, word: str) -> bool:
        if self.is_keyword(word):
            self.advance()
            return True
        return False

    def expect_keyword(self, word: str) -> None:
        if not self.accept_keyword(word):
            self.fail(word)

    def is_word(self, word: str, token: Token | None = None) -> bool:
        """A contextual word: an unquoted identifier with this spelling."""
        token = self.current if token is None else token
        return (
            token.type is TokenType.IDENT
            and not token.text.startswith('"')
            and token.text.upper() == word
        )

    def accept_word(self, word: str) -> bool:
        if self.is_word(word):
            self.advance()
            return True
        return False

    def expect_word(self, word: str) -> None:
        if not self.accept_word(word):
            self.fail(word)

    def identifier(self, what: str = "identifier") -> str:
        token = self.current
        if token.type is not TokenType.IDENT:
            self.fail(what)
        self.advance()
        assert isinstance(token.value, str)
        return token.value

    def comma_list(self, item: Callable[[], T]) -> list[T]:
        items = [item()]
        while self.accept(TokenType.COMMA):
            items.append(item())
        return items

    def expect_end(self) -> None:
        if not self.at_end():
            self.fail("end of statement")

    # ----------------------------------------------------------------- statements

    def statement(self) -> ast.Statement:
        token = self.current
        if self.is_keyword("SELECT"):
            return self.select()
        if self.is_keyword("INSERT"):
            return self.insert()
        if self.is_keyword("UPDATE"):
            return self.update()
        if self.is_keyword("DELETE"):
            return self.delete()
        if self.is_keyword("CREATE"):
            return self.create()
        if self.is_keyword("DROP"):
            return self.drop()
        if self.accept_word("BEGIN"):
            return ast.Begin()
        if self.accept_word("COMMIT"):
            return ast.Commit()
        if self.accept_word("ROLLBACK"):
            return ast.Rollback()
        if self.accept_word("EXPLAIN"):
            analyze = self.accept_word("ANALYZE")
            if not self.is_keyword("SELECT"):
                self.fail("SELECT after EXPLAIN")
            return ast.Explain(self.select(), analyze)
        if self.accept_word("SHOW"):
            self.expect_word("TABLES")
            return ast.ShowTables()
        if self.accept_word("DESCRIBE"):
            return ast.Describe(self.identifier("table name"))
        if self.accept_word("ANALYZE"):
            return ast.Analyze(self.identifier("table name"))
        self.fail("a statement", token)

    def select(self) -> ast.Select:
        self.expect_keyword("SELECT")
        distinct = self.accept_keyword("DISTINCT")
        items = tuple(self.comma_list(self.select_item))
        from_table: ast.TableRef | None = None
        joins: list[ast.Join] = []
        if self.at_identifier():
            self.fail("FROM")
        if self.accept_keyword("FROM"):
            from_table = self.table_ref()
            while (join := self.join()) is not None:
                joins.append(join)
            if self.at_identifier():
                self.fail("WHERE, JOIN, GROUP BY, ORDER BY, LIMIT or end of statement")
        where = self.expression() if self.accept_keyword("WHERE") else None
        group_by: tuple[ast.Expr, ...] = ()
        having = None
        if self.accept_keyword("GROUP"):
            self.expect_keyword("BY")
            group_by = tuple(self.comma_list(self.expression))
            if self.accept_keyword("HAVING"):
                having = self.expression()
        order_by: tuple[ast.OrderItem, ...] = ()
        if self.accept_keyword("ORDER"):
            self.expect_keyword("BY")
            order_by = tuple(self.comma_list(self.order_item))
        limit = offset = None
        if self.accept_keyword("LIMIT"):
            limit = self.non_negative_integer("LIMIT count")
            if self.accept_keyword("OFFSET"):
                offset = self.non_negative_integer("OFFSET count")
        return ast.Select(
            items=items,
            from_table=from_table,
            joins=tuple(joins),
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
            distinct=distinct,
        )

    def select_item(self) -> ast.SelectItem:
        if self.accept(TokenType.STAR):
            return ast.SelectItem(ast.Star())
        if (
            self.current.type is TokenType.IDENT
            and self.peek().type is TokenType.DOT
            and self.peek(2).type is TokenType.STAR
        ):
            table = self.identifier()
            self.advance()
            self.advance()
            return ast.SelectItem(ast.Star(table))
        return ast.SelectItem(self.expression(), self.alias())

    def alias(self) -> str | None:
        """``AS name`` or a bare ``name``.

        A bare name followed by another identifier is not taken as an alias: in valid SQL an
        alias is followed by a keyword or punctuation, so ``SELECT a FORM t`` is a misspelt
        keyword, reported as such, rather than ``a`` aliased to ``form``.
        """
        if self.accept_keyword("AS"):
            return self.identifier("alias")
        if self.current.type is TokenType.IDENT and self.peek().type is not TokenType.IDENT:
            return self.identifier()
        return None

    def table_ref(self) -> ast.TableRef:
        return ast.TableRef(self.identifier("table name"), self.alias())

    def join(self) -> ast.Join | None:
        if self.accept_keyword("JOIN"):
            kind = "INNER"
        elif self.accept_keyword("INNER"):
            self.expect_keyword("JOIN")
            kind = "INNER"
        elif self.accept_keyword("LEFT"):
            self.accept_keyword("OUTER")
            self.expect_keyword("JOIN")
            kind = "LEFT"
        else:
            return None
        table = self.table_ref()
        self.expect_keyword("ON")
        return ast.Join(kind, table, self.expression())

    def order_item(self) -> ast.OrderItem:
        expr = self.expression()
        if self.accept_keyword("DESC"):
            return ast.OrderItem(expr, descending=True)
        self.accept_keyword("ASC")
        return ast.OrderItem(expr)

    def non_negative_integer(self, what: str) -> int:
        if self.current.type is TokenType.PARAM:
            value = self.parameter()
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ParameterError(f"{what} must be a non-negative integer, got {value!r}")
            return value
        token = self.expect(TokenType.INTEGER, what)
        assert isinstance(token.value, int)
        return token.value

    def insert(self) -> ast.Insert:
        self.expect_keyword("INSERT")
        self.expect_keyword("INTO")
        table = self.identifier("table name")
        columns = None
        if self.accept(TokenType.LPAREN):
            columns = tuple(self.comma_list(lambda: self.identifier("column name")))
            self.expect(TokenType.RPAREN, "')'")
        self.expect_keyword("VALUES")
        rows = tuple(self.comma_list(self.value_row))
        return ast.Insert(table, rows, columns)

    def value_row(self) -> tuple[ast.Expr, ...]:
        self.expect(TokenType.LPAREN, "'(' before a row of values")
        row = tuple(self.comma_list(self.expression))
        self.expect(TokenType.RPAREN, "')'")
        return row

    def update(self) -> ast.Update:
        self.expect_keyword("UPDATE")
        table = self.identifier("table name")
        self.expect_keyword("SET")

        def assignment() -> tuple[str, ast.Expr]:
            column = self.identifier("column name")
            token = self.current
            if not (token.type is TokenType.OPERATOR and token.value == "="):
                self.fail("'='")
            self.advance()
            return column, self.expression()

        assignments = tuple(self.comma_list(assignment))
        where = self.expression() if self.accept_keyword("WHERE") else None
        return ast.Update(table, assignments, where)

    def delete(self) -> ast.Delete:
        self.expect_keyword("DELETE")
        self.expect_keyword("FROM")
        table = self.identifier("table name")
        where = self.expression() if self.accept_keyword("WHERE") else None
        return ast.Delete(table, where)

    def create(self) -> ast.CreateTable | ast.CreateIndex:
        self.expect_keyword("CREATE")
        if self.accept_keyword("TABLE"):
            return self.create_table()
        unique = self.accept_keyword("UNIQUE")
        if self.accept_keyword("INDEX"):
            name = self.identifier("index name")
            self.expect_keyword("ON")
            table = self.identifier("table name")
            return ast.CreateIndex(name, table, self.column_list(), unique)
        self.fail("INDEX" if unique else "TABLE or INDEX")

    def column_list(self) -> tuple[str, ...]:
        self.expect(TokenType.LPAREN, "'('")
        columns = tuple(self.comma_list(lambda: self.identifier("column name")))
        self.expect(TokenType.RPAREN, "')'")
        return columns

    def create_table(self) -> ast.CreateTable:
        if_not_exists = False
        if self.is_word("IF") and self.is_keyword("NOT", token=self.peek()):
            self.advance()
            self.expect_keyword("NOT")
            self.expect_word("EXISTS")
            if_not_exists = True
        name = self.identifier("table name")
        self.expect(TokenType.LPAREN, "'('")
        columns: list[ast.ColumnDef] = []
        primary_key: tuple[str, ...] = ()
        while True:
            if self.accept_keyword("PRIMARY"):
                self.expect_word("KEY")
                if primary_key:
                    self.fail("one table-level PRIMARY KEY", self.tokens[self.pos - 2])
                primary_key = self.column_list()
            else:
                if primary_key:
                    self.fail("')' after the table-level PRIMARY KEY")
                columns.append(self.column_def())
            if not self.accept(TokenType.COMMA):
                break
        self.expect(TokenType.RPAREN, "',' or ')'")
        if not columns:
            self.fail("at least one column definition")
        return ast.CreateTable(name, tuple(columns), primary_key, if_not_exists)

    def column_def(self) -> ast.ColumnDef:
        name = self.identifier("column name")
        type_name = self.type_name()
        not_null = primary_key = unique = False
        default: ast.Literal | None = None
        seen: set[str] = set()
        while True:
            start = self.current
            if self.accept_keyword("NOT"):
                self.expect_keyword("NULL")
                constraint, not_null = "NOT NULL", True
            elif self.accept_keyword("PRIMARY"):
                self.expect_word("KEY")
                constraint, primary_key = "PRIMARY KEY", True
            elif self.accept_keyword("UNIQUE"):
                constraint, unique = "UNIQUE", True
            elif self.accept_keyword("DEFAULT"):
                constraint, default = "DEFAULT", self.default_literal()
            else:
                break
            if constraint in seen:
                raise SqlSyntaxError(f"{constraint} given twice", start.line, start.column)
            seen.add(constraint)
        return ast.ColumnDef(name, type_name, not_null, primary_key, unique, default)

    def type_name(self) -> ast.TypeName:
        token = self.current
        word = token.text.upper() if token.type is TokenType.IDENT else ""
        if word == "VARCHAR":
            self.advance()
            self.expect(TokenType.LPAREN, "'(' after VARCHAR")
            length_token = self.current
            length = self.non_negative_integer("VARCHAR length")
            if length == 0:
                self.fail("a positive VARCHAR length", length_token)
            self.expect(TokenType.RPAREN, "')'")
            return ast.TypeName(DataType.TEXT, length)
        if word in _TYPE_NAMES:
            self.advance()
            return ast.TypeName(_TYPE_NAMES[word])
        self.fail("a type (INT, FLOAT, TEXT, VARCHAR(n) or BOOL)")

    def default_literal(self) -> ast.Literal:
        expr = self.prefix()
        if not isinstance(expr, ast.Literal):
            self.fail("a literal after DEFAULT", self.tokens[self.pos - 1])
        return expr

    def drop(self) -> ast.DropTable | ast.DropIndex:
        self.expect_keyword("DROP")
        if self.accept_keyword("TABLE"):
            if_exists = self.if_exists()
            return ast.DropTable(self.identifier("table name"), if_exists)
        if self.accept_keyword("INDEX"):
            if_exists = self.if_exists()
            return ast.DropIndex(self.identifier("index name"), if_exists)
        self.fail("TABLE or INDEX")

    def if_exists(self) -> bool:
        if self.is_word("IF") and self.is_word("EXISTS", token=self.peek()):
            self.advance()
            self.advance()
            return True
        return False

    # ----------------------------------------------------------------- expressions (Pratt)

    def expression(self, min_bp: int = 0) -> ast.Expr:
        left = self.prefix()
        while True:
            token = self.current
            op = self.infix_operator(token)
            if op is None or _INFIX_POWER[op] <= min_bp:
                return left
            left = self.infix(left, op)

    def infix_operator(self, token: Token) -> str | None:
        if token.type is TokenType.OPERATOR:
            assert isinstance(token.value, str)
            return token.value
        if token.type is TokenType.STAR:
            return "*"
        if token.type is TokenType.KEYWORD and token.value in _INFIX_POWER:
            if token.value == "NOT" and not self.is_keyword(
                "IN", "BETWEEN", "LIKE", token=self.peek()
            ):
                return None
            assert isinstance(token.value, str)
            return token.value
        return None

    def infix(self, left: ast.Expr, op: str) -> ast.Expr:
        self.advance()
        power = _INFIX_POWER[op]
        if op == "IS":
            negated = self.accept_keyword("NOT")
            self.expect_keyword("NULL")
            return ast.IsNull(left, negated)
        negated = False
        if op == "NOT":
            negated = True
            op = str(self.advance().value)
        if op == "IN":
            self.expect(TokenType.LPAREN, "'(' after IN")
            items = tuple(self.comma_list(self.expression))
            self.expect(TokenType.RPAREN, "')'")
            return ast.InList(left, items, negated)
        if op == "BETWEEN":
            low = self.expression(power)
            self.expect_keyword("AND")
            return ast.Between(left, low, self.expression(power), negated)
        if op == "LIKE":
            return ast.Like(left, self.expression(power), negated)
        return ast.BinaryOp(op, left, self.expression(power))

    def prefix(self) -> ast.Expr:
        token = self.current
        if token.type is TokenType.PARAM:
            return ast.Literal(self.parameter())
        if token.type is TokenType.INTEGER or token.type is TokenType.FLOAT:
            self.advance()
            assert isinstance(token.value, int | float)
            return ast.Literal(token.value)
        if token.type is TokenType.STRING:
            self.advance()
            assert isinstance(token.value, str)
            return ast.Literal(token.value)
        if self.accept_keyword("NULL"):
            return ast.Literal(None)
        if self.accept_keyword("TRUE"):
            return ast.Literal(True)
        if self.accept_keyword("FALSE"):
            return ast.Literal(False)
        if token.type is TokenType.OPERATOR and token.value == "-":
            self.advance()
            following = self.current
            if following.type is TokenType.INTEGER or following.type is TokenType.FLOAT:
                self.advance()
                assert isinstance(following.value, int | float)
                return ast.Literal(-following.value)
            return ast.UnaryOp("-", self.expression(_BP_UNARY))
        if token.type is TokenType.OPERATOR and token.value == "+":
            self.advance()
            return self.expression(_BP_UNARY)
        if self.accept_keyword("NOT"):
            return ast.UnaryOp("NOT", self.expression(_BP_NOT))
        if self.accept(TokenType.LPAREN):
            inner = self.expression()
            self.expect(TokenType.RPAREN, "')'")
            return inner
        if token.type is TokenType.IDENT:
            return self.name_or_call()
        self.fail("an expression")

    def name_or_call(self) -> ast.Expr:
        name = self.identifier()
        if self.accept(TokenType.LPAREN):
            function = name.upper()
            if self.accept(TokenType.STAR):
                if function != "COUNT":
                    self.fail(
                        "an argument ('*' is only allowed in COUNT)", self.tokens[self.pos - 1]
                    )
                self.expect(TokenType.RPAREN, "')'")
                return ast.FunctionCall(function, (), star=True)
            args: tuple[ast.Expr, ...] = ()
            if not self.accept(TokenType.RPAREN):
                args = tuple(self.comma_list(self.expression))
                self.expect(TokenType.RPAREN, "')'")
            return ast.FunctionCall(function, args)
        if self.accept(TokenType.DOT):
            return ast.ColumnRef(self.identifier("column name"), table=name)
        return ast.ColumnRef(name)
