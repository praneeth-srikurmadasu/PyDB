"""Abstract syntax tree for the supported SQL subset.

Nodes are frozen dataclasses, compared by value. ``to_sql()`` renders a node back to SQL
that parses to an equal node: expressions are fully parenthesised, identifiers are quoted
when they would not survive unquoted, and strings escape their quotes.
"""

import re
from dataclasses import dataclass

from pydb.sql.tokens import RESERVED_KEYWORDS
from pydb.types import DataType

LiteralValue = int | float | str | bool | None

_BARE_IDENT = re.compile(r"[a-z_][a-z0-9_]*\Z")


def quote_ident(name: str) -> str:
    if _BARE_IDENT.match(name) and name.upper() not in RESERVED_KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------- expressions


@dataclass(frozen=True, slots=True)
class Literal:
    value: LiteralValue

    def to_sql(self) -> str:
        value = self.value
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, str):
            return quote_string(value)
        return repr(value)


@dataclass(frozen=True, slots=True)
class ColumnRef:
    name: str
    table: str | None = None

    def to_sql(self) -> str:
        column = quote_ident(self.name)
        return column if self.table is None else f"{quote_ident(self.table)}.{column}"


@dataclass(frozen=True, slots=True)
class Star:
    """``*`` or ``table.*`` in a select list."""

    table: str | None = None

    def to_sql(self) -> str:
        return "*" if self.table is None else f"{quote_ident(self.table)}.*"


@dataclass(frozen=True, slots=True)
class UnaryOp:
    op: str  # "-" or "NOT"
    operand: "Expr"

    def to_sql(self) -> str:
        return f"({self.op} {self.operand.to_sql()})"


@dataclass(frozen=True, slots=True)
class BinaryOp:
    op: str  # OR AND = != < <= > >= || + - * / %
    left: "Expr"
    right: "Expr"

    def to_sql(self) -> str:
        return f"({self.left.to_sql()} {self.op} {self.right.to_sql()})"


@dataclass(frozen=True, slots=True)
class IsNull:
    operand: "Expr"
    negated: bool = False

    def to_sql(self) -> str:
        return f"({self.operand.to_sql()} IS {'NOT ' if self.negated else ''}NULL)"


@dataclass(frozen=True, slots=True)
class InList:
    operand: "Expr"
    items: tuple["Expr", ...]
    negated: bool = False

    def to_sql(self) -> str:
        items = ", ".join(item.to_sql() for item in self.items)
        return f"({self.operand.to_sql()} {'NOT ' if self.negated else ''}IN ({items}))"


@dataclass(frozen=True, slots=True)
class Between:
    operand: "Expr"
    low: "Expr"
    high: "Expr"
    negated: bool = False

    def to_sql(self) -> str:
        keyword = "NOT BETWEEN" if self.negated else "BETWEEN"
        return f"({self.operand.to_sql()} {keyword} {self.low.to_sql()} AND {self.high.to_sql()})"


@dataclass(frozen=True, slots=True)
class Like:
    operand: "Expr"
    pattern: "Expr"
    negated: bool = False

    def to_sql(self) -> str:
        keyword = "NOT LIKE" if self.negated else "LIKE"
        return f"({self.operand.to_sql()} {keyword} {self.pattern.to_sql()})"


@dataclass(frozen=True, slots=True)
class FunctionCall:
    name: str  # upper case
    args: tuple["Expr", ...] = ()
    star: bool = False  # COUNT(*)

    def to_sql(self) -> str:
        inner = "*" if self.star else ", ".join(arg.to_sql() for arg in self.args)
        return f"{self.name}({inner})"


Expr = Literal | ColumnRef | UnaryOp | BinaryOp | IsNull | InList | Between | Like | FunctionCall


# --------------------------------------------------------------------- SELECT


@dataclass(frozen=True, slots=True)
class SelectItem:
    expr: Expr | Star
    alias: str | None = None

    def to_sql(self) -> str:
        sql = self.expr.to_sql()
        return sql if self.alias is None else f"{sql} AS {quote_ident(self.alias)}"


@dataclass(frozen=True, slots=True)
class TableRef:
    name: str
    alias: str | None = None

    def to_sql(self) -> str:
        sql = quote_ident(self.name)
        return sql if self.alias is None else f"{sql} AS {quote_ident(self.alias)}"


@dataclass(frozen=True, slots=True)
class Join:
    kind: str  # "INNER" or "LEFT"
    table: TableRef
    on: Expr

    def to_sql(self) -> str:
        return f"{self.kind} JOIN {self.table.to_sql()} ON {self.on.to_sql()}"


@dataclass(frozen=True, slots=True)
class OrderItem:
    expr: Expr
    descending: bool = False

    def to_sql(self) -> str:
        return f"{self.expr.to_sql()} {'DESC' if self.descending else 'ASC'}"


@dataclass(frozen=True, slots=True)
class Select:
    items: tuple[SelectItem, ...]
    from_table: TableRef | None = None
    joins: tuple[Join, ...] = ()
    where: Expr | None = None
    group_by: tuple[Expr, ...] = ()
    having: Expr | None = None
    order_by: tuple[OrderItem, ...] = ()
    limit: int | None = None
    offset: int | None = None
    distinct: bool = False

    def to_sql(self) -> str:
        parts = ["SELECT"]
        if self.distinct:
            parts.append("DISTINCT")
        parts.append(", ".join(item.to_sql() for item in self.items))
        if self.from_table is not None:
            parts.append(f"FROM {self.from_table.to_sql()}")
        parts.extend(join.to_sql() for join in self.joins)
        if self.where is not None:
            parts.append(f"WHERE {self.where.to_sql()}")
        if self.group_by:
            parts.append("GROUP BY " + ", ".join(e.to_sql() for e in self.group_by))
        if self.having is not None:
            parts.append(f"HAVING {self.having.to_sql()}")
        if self.order_by:
            parts.append("ORDER BY " + ", ".join(o.to_sql() for o in self.order_by))
        if self.limit is not None:
            parts.append(f"LIMIT {self.limit}")
        if self.offset is not None:
            parts.append(f"OFFSET {self.offset}")
        return " ".join(parts)


# --------------------------------------------------------------------- DDL


@dataclass(frozen=True, slots=True)
class TypeName:
    base: DataType
    length: int | None = None  # VARCHAR(n): a maximum length in characters

    def to_sql(self) -> str:
        if self.length is not None:
            return f"VARCHAR({self.length})"
        return self.base.name


@dataclass(frozen=True, slots=True)
class ColumnDef:
    name: str
    type: TypeName
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False
    default: Literal | None = None

    def to_sql(self) -> str:
        parts = [quote_ident(self.name), self.type.to_sql()]
        if self.not_null:
            parts.append("NOT NULL")
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if self.unique:
            parts.append("UNIQUE")
        if self.default is not None:
            parts.append(f"DEFAULT {self.default.to_sql()}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class CreateTable:
    name: str
    columns: tuple[ColumnDef, ...]
    primary_key: tuple[str, ...] = ()  # table-level PRIMARY KEY (...)
    if_not_exists: bool = False

    def to_sql(self) -> str:
        items = [c.to_sql() for c in self.columns]
        if self.primary_key:
            items.append("PRIMARY KEY (" + ", ".join(map(quote_ident, self.primary_key)) + ")")
        guard = "IF NOT EXISTS " if self.if_not_exists else ""
        return f"CREATE TABLE {guard}{quote_ident(self.name)} ({', '.join(items)})"


@dataclass(frozen=True, slots=True)
class DropTable:
    name: str
    if_exists: bool = False

    def to_sql(self) -> str:
        return f"DROP TABLE {'IF EXISTS ' if self.if_exists else ''}{quote_ident(self.name)}"


@dataclass(frozen=True, slots=True)
class CreateIndex:
    name: str
    table: str
    columns: tuple[str, ...]
    unique: bool = False

    def to_sql(self) -> str:
        cols = ", ".join(map(quote_ident, self.columns))
        kind = "UNIQUE INDEX" if self.unique else "INDEX"
        return f"CREATE {kind} {quote_ident(self.name)} ON {quote_ident(self.table)} ({cols})"


@dataclass(frozen=True, slots=True)
class DropIndex:
    name: str
    if_exists: bool = False

    def to_sql(self) -> str:
        return f"DROP INDEX {'IF EXISTS ' if self.if_exists else ''}{quote_ident(self.name)}"


# --------------------------------------------------------------------- DML


@dataclass(frozen=True, slots=True)
class Insert:
    table: str
    rows: tuple[tuple[Expr, ...], ...]
    columns: tuple[str, ...] | None = None

    def to_sql(self) -> str:
        cols = (
            "" if self.columns is None else " (" + ", ".join(map(quote_ident, self.columns)) + ")"
        )
        rows = ", ".join("(" + ", ".join(e.to_sql() for e in row) + ")" for row in self.rows)
        return f"INSERT INTO {quote_ident(self.table)}{cols} VALUES {rows}"


@dataclass(frozen=True, slots=True)
class Update:
    table: str
    assignments: tuple[tuple[str, Expr], ...]
    where: Expr | None = None

    def to_sql(self) -> str:
        sets = ", ".join(f"{quote_ident(c)} = {e.to_sql()}" for c, e in self.assignments)
        sql = f"UPDATE {quote_ident(self.table)} SET {sets}"
        return sql if self.where is None else f"{sql} WHERE {self.where.to_sql()}"


@dataclass(frozen=True, slots=True)
class Delete:
    table: str
    where: Expr | None = None

    def to_sql(self) -> str:
        sql = f"DELETE FROM {quote_ident(self.table)}"
        return sql if self.where is None else f"{sql} WHERE {self.where.to_sql()}"


# --------------------------------------------------------------------- other statements


@dataclass(frozen=True, slots=True)
class Begin:
    def to_sql(self) -> str:
        return "BEGIN"


@dataclass(frozen=True, slots=True)
class Commit:
    def to_sql(self) -> str:
        return "COMMIT"


@dataclass(frozen=True, slots=True)
class Rollback:
    def to_sql(self) -> str:
        return "ROLLBACK"


@dataclass(frozen=True, slots=True)
class Explain:
    query: Select
    analyze: bool = False

    def to_sql(self) -> str:
        return f"EXPLAIN {'ANALYZE ' if self.analyze else ''}{self.query.to_sql()}"


@dataclass(frozen=True, slots=True)
class ShowTables:
    def to_sql(self) -> str:
        return "SHOW TABLES"


@dataclass(frozen=True, slots=True)
class Describe:
    table: str

    def to_sql(self) -> str:
        return f"DESCRIBE {quote_ident(self.table)}"


@dataclass(frozen=True, slots=True)
class Analyze:
    table: str

    def to_sql(self) -> str:
        return f"ANALYZE {quote_ident(self.table)}"


Statement = (
    Select
    | CreateTable
    | DropTable
    | CreateIndex
    | DropIndex
    | Insert
    | Update
    | Delete
    | Begin
    | Commit
    | Rollback
    | Explain
    | ShowTables
    | Describe
    | Analyze
)
