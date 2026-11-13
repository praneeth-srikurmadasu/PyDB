"""Turns SQL text into tokens that carry their line and column (both 1-based)."""

from pydb.errors import SqlSyntaxError
from pydb.sql.tokens import RESERVED_KEYWORDS, Token, TokenType

_PUNCTUATION = {
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    ",": TokenType.COMMA,
    ".": TokenType.DOT,
    ";": TokenType.SEMICOLON,
    "*": TokenType.STAR,
}
_TWO_CHAR_OPERATORS = {"!=": "!=", "<>": "!=", "<=": "<=", ">=": ">=", "||": "||"}
_ONE_CHAR_OPERATORS = set("=<>+-/%")


def tokenize(sql: str) -> list[Token]:
    return _Lexer(sql).run()


class _Lexer:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []

    def run(self) -> list[Token]:
        while True:
            self._skip_space_and_comments()
            if self.pos >= len(self.sql):
                self.tokens.append(Token(TokenType.EOF, "", None, self.line, self.column))
                return self.tokens
            self._next_token()

    # ----------------------------------------------------------------- scanning helpers

    def _peek(self, offset: int = 0) -> str:
        i = self.pos + offset
        return self.sql[i] if i < len(self.sql) else ""

    def _advance(self, count: int = 1) -> None:
        for _ in range(count):
            if self.sql[self.pos] == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
            self.pos += 1

    def _error(self, message: str, line: int | None = None, column: int | None = None) -> None:
        raise SqlSyntaxError(
            message, self.line if line is None else line, self.column if column is None else column
        )

    def _skip_space_and_comments(self) -> None:
        while self.pos < len(self.sql):
            ch = self._peek()
            if ch.isspace():
                self._advance()
            elif ch == "-" and self._peek(1) == "-":
                while self.pos < len(self.sql) and self._peek() != "\n":
                    self._advance()
            elif ch == "/" and self._peek(1) == "*":
                line, column = self.line, self.column
                self._advance(2)
                while not (self._peek() == "*" and self._peek(1) == "/"):
                    if self.pos >= len(self.sql):
                        self._error("unterminated /* comment", line, column)
                    self._advance()
                self._advance(2)
            else:
                return

    def _emit(self, type_: TokenType, start: int, line: int, column: int, value: object) -> None:
        self.tokens.append(Token(type_, self.sql[start : self.pos], value, line, column))

    # ----------------------------------------------------------------- tokens

    def _next_token(self) -> None:
        start, line, column = self.pos, self.line, self.column
        ch = self._peek()
        if ch.isalpha() or ch == "_":
            while self._peek().isalnum() or self._peek() == "_":
                self._advance()
            word = self.sql[start : self.pos]
            upper = word.upper()
            if upper in RESERVED_KEYWORDS:
                self._emit(TokenType.KEYWORD, start, line, column, upper)
            else:
                self._emit(TokenType.IDENT, start, line, column, word.lower())
        elif ch == '"':
            name = self._quoted('"', "identifier", line, column)
            if not name:
                self._error("empty quoted identifier", line, column)
            self._emit(TokenType.IDENT, start, line, column, name)
        elif ch == "'":
            self._emit(
                TokenType.STRING, start, line, column, self._quoted("'", "string", line, column)
            )
        elif ch.isdigit() or (ch == "." and self._peek(1).isdigit()):
            self._number(start, line, column)
        elif self.sql[self.pos : self.pos + 2] in _TWO_CHAR_OPERATORS:
            op = _TWO_CHAR_OPERATORS[self.sql[self.pos : self.pos + 2]]
            self._advance(2)
            self._emit(TokenType.OPERATOR, start, line, column, op)
        elif ch in _ONE_CHAR_OPERATORS:
            self._advance()
            self._emit(TokenType.OPERATOR, start, line, column, ch)
        elif ch in _PUNCTUATION:
            self._advance()
            self._emit(_PUNCTUATION[ch], start, line, column, ch)
        else:
            self._error(f"unexpected character {ch!r}")

    def _quoted(self, quote: str, what: str, line: int, column: int) -> str:
        """Read a quoted string or identifier; a doubled quote stands for one quote."""
        self._advance()
        parts: list[str] = []
        while True:
            if self.pos >= len(self.sql):
                self._error(f"unterminated {what}", line, column)
            ch = self._peek()
            if ch == quote:
                if self._peek(1) == quote:
                    parts.append(quote)
                    self._advance(2)
                    continue
                self._advance()
                return "".join(parts)
            parts.append(ch)
            self._advance()

    def _number(self, start: int, line: int, column: int) -> None:
        is_float = False
        while self._peek().isdigit():
            self._advance()
        if self._peek() == "." and (self.pos > start or self._peek(1).isdigit()):
            is_float = True
            self._advance()
            while self._peek().isdigit():
                self._advance()
        if self._peek() in ("e", "E"):
            sign = 1 if self._peek(1) in ("+", "-") else 0
            if self._peek(1 + sign).isdigit():
                is_float = True
                self._advance(1 + sign)
                while self._peek().isdigit():
                    self._advance()
        if self._peek().isalpha() or self._peek() == "_":
            self._error(f"invalid number {self.sql[start : self.pos + 1]!r}", line, column)
        text = self.sql[start : self.pos]
        if is_float:
            self._emit(TokenType.FLOAT, start, line, column, float(text))
        else:
            self._emit(TokenType.INTEGER, start, line, column, int(text))
