"""Token types and keywords."""

from dataclasses import dataclass
from enum import Enum, auto


class TokenType(Enum):
    IDENT = auto()  # value: the name (lower-cased unless quoted)
    KEYWORD = auto()  # value: the keyword in upper case
    INTEGER = auto()  # value: int
    FLOAT = auto()  # value: float
    STRING = auto()  # value: str with '' unescaped
    OPERATOR = auto()  # value: the operator text; <> is normalised to !=
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    DOT = auto()
    SEMICOLON = auto()
    STAR = auto()
    EOF = auto()


RESERVED_KEYWORDS = frozenset(
    {
        "AND", "AS", "ASC", "BETWEEN", "BY", "CREATE", "DEFAULT", "DELETE", "DESC", "DISTINCT",
        "DROP", "FALSE", "FROM", "GROUP", "HAVING", "IN", "INDEX", "INNER", "INSERT", "INTO",
        "IS", "JOIN", "LEFT", "LIKE", "LIMIT", "NOT", "NULL", "OFFSET", "ON", "OR", "ORDER",
        "OUTER", "PRIMARY", "SELECT", "SET", "TABLE", "TRUE", "UNIQUE", "UPDATE", "VALUES",
        "WHERE",
    }
)  # fmt: skip
"""Words that can never be unquoted identifiers.

Other words the grammar uses (KEY, IF, EXISTS, BEGIN, COMMIT, ROLLBACK, EXPLAIN, ANALYZE,
SHOW, TABLES, DESCRIBE and the type names) are recognised by position, so they remain
usable as table and column names.
"""


@dataclass(frozen=True, slots=True)
class Token:
    type: TokenType
    text: str  # the source text of the token
    value: object
    line: int
    column: int

    def describe(self) -> str:
        """How the token is named in error messages."""
        if self.type is TokenType.EOF:
            return "end of input"
        return repr(self.text)
